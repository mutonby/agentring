package dev.agentring.app.data

import android.content.Context
import android.util.Log
import dev.agentring.core.Event
import dev.agentring.core.Message
import dev.agentring.core.ProtocolJson
import dev.agentring.core.RelayApi
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.update
import kotlinx.serialization.builtins.ListSerializer
import java.io.File

/**
 * The conversation with the agent. The relay is the source of truth; this is
 * an in-memory list plus a small JSON cache so the thread shows instantly.
 */
class MessageRepository(context: Context) {
    private val cache = File(context.filesDir, "messages.json")
    private val prefs = context.getSharedPreferences("agentring_state", Context.MODE_PRIVATE)
    private val serializer = ListSerializer(Message.serializer())

    private val _messages = MutableStateFlow(loadCache())
    val messages: StateFlow<List<Message>> = _messages

    /** Highest relay event `seq` applied from the WebSocket stream. */
    val lastSeq: Long get() = prefs.getLong(KEY_SEQ, 0L)

    /**
     * Moves on with every [clear]. Work for a pairing reads it before starting and passes
     * it back with what it writes, so a late answer can't reach the next pairing's store.
     */
    @Volatile var generation = 0L
        private set

    private fun loadCache(): List<Message> = try {
        if (cache.exists()) ProtocolJson.decodeFromString(serializer, cache.readText()) else emptyList()
    } catch (e: Exception) {
        Log.w(TAG, "dropping unreadable message cache", e)
        emptyList()
    }

    private fun persist(list: List<Message>) {
        try {
            val tmp = File(cache.parentFile, cache.name + ".tmp")
            tmp.writeText(ProtocolJson.encodeToString(serializer, list.takeLast(MAX_CACHED)))
            tmp.renameTo(cache)
        } catch (e: Exception) {
            Log.w(TAG, "cannot write message cache", e)
        }
    }

    private fun mutate(generation: Long = this.generation, block: (List<Message>) -> List<Message>) {
        synchronized(this) {
            if (generation != this.generation) return
            _messages.update { block(it) }
            persist(_messages.value)
        }
    }

    /** Inserts or updates by msg_id. A truncated push never overwrites a full text we already have. */
    fun upsert(m: Message, truncated: Boolean = false, generation: Long = this.generation) = mutate(generation) { list ->
        val existing = list.firstOrNull { it.msgId == m.msgId }
        val merged = when {
            existing == null -> m
            truncated -> existing.copy(readAt = existing.readAt ?: m.readAt)
            else -> m.copy(readAt = m.readAt ?: existing.readAt)
        }
        (list.filter { it.msgId != m.msgId } + merged).sortedBy { it.msgId }
    }

    /** Applies a relay event. `advanceSeq` is false for push-delivered events (the stream may still be behind). */
    fun apply(event: Event, advanceSeq: Boolean, generation: Long = this.generation) {
        when (event) {
            is Event.MsgNew -> upsert(event.toMessage(), truncated = event.truncated == true, generation = generation)
            is Event.MsgRead -> mutate(generation) { list ->
                list.map { if (it.msgId == event.msgId && it.readAt == null) it.copy(readAt = event.ts) else it }
            }
            else -> Unit
        }
        val seq = event.seq
        synchronized(this) {
            if (generation == this.generation && advanceSeq && seq != null && seq > lastSeq) {
                prefs.edit().putLong(KEY_SEQ, seq).apply()
            }
        }
    }

    /** Pulls messages we don't have yet. Blocking; call from a background thread. */
    fun refresh(api: RelayApi, generation: Long = this.generation) {
        var after = _messages.value.lastOrNull()?.msgId
        while (generation == this.generation) {
            val page = api.listMessages(after = after)
            page.messages.forEach { upsert(it, generation = generation) }
            if (!page.hasMore || page.messages.isEmpty()) return
            after = page.messages.last().msgId
        }
    }

    /** Sends read receipts for the agent's messages. Blocking. */
    fun markAgentMessagesRead(api: RelayApi) {
        val unread = _messages.value.filter { it.fromAgent && it.readAt == null }
        val now = System.currentTimeMillis()
        for (m in unread) {
            try {
                api.markRead(m.msgId)
                mutate { list -> list.map { if (it.msgId == m.msgId) it.copy(readAt = now) else it } }
            } catch (e: Exception) {
                Log.w(TAG, "markRead failed", e)
                return
            }
        }
    }

    fun unreadFromAgent(): List<Message> = _messages.value.filter { it.fromAgent && it.readAt == null }

    fun clear() = synchronized(this) {
        generation++
        _messages.value = emptyList()
        cache.delete()
        prefs.edit().remove(KEY_SEQ).apply()
    }

    private companion object {
        const val TAG = "MessageRepository"
        const val KEY_SEQ = "last_seq"
        const val MAX_CACHED = 500
    }
}
