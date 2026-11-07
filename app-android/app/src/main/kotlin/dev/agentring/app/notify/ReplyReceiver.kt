package dev.agentring.app.notify

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.util.Log
import androidx.core.app.RemoteInput
import dev.agentring.app.Graph
import dev.agentring.app.R
import dev.agentring.core.RelayException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import java.util.UUID

/** Inline reply from the message notification. */
class ReplyReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        val text = RemoteInput.getResultsFromIntent(intent)?.getCharSequence(Notifications.KEY_REPLY)?.toString()?.trim()
        if (text.isNullOrEmpty()) return
        val appContext = context.applicationContext
        val pending = goAsync()
        Graph.appScope.launch(Dispatchers.IO) {
            try {
                val generation = Graph.messages.generation
                val api = Graph.api() ?: error("not paired")
                val agentName = Graph.pairing.current?.agentName ?: ""
                val shown = Graph.messages.unreadFromAgent()
                Graph.messages.upsert(api.postMessage(text, clientMsgId = UUID.randomUUID().toString()), generation = generation)
                // Replying implies the user read what the agent said.
                Graph.messages.markAgentMessagesRead(api)
                Notifications.showMessages(appContext, agentName, shown, sentReply = text)
            } catch (e: Exception) {
                Log.w("ReplyReceiver", "reply failed", e)
                Notifications.cancelMessages(appContext)
                val why = if (e is RelayException && e.code == "quota_exceeded") appContext.getString(R.string.message_quota)
                else e.message ?: e.javaClass.simpleName
                Notifications.showReplyFailed(appContext, text, why)
            } finally {
                pending.finish()
            }
        }
    }
}
