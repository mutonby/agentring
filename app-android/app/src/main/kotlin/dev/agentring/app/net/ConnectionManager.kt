package dev.agentring.app.net

import android.util.Log
import androidx.lifecycle.DefaultLifecycleObserver
import androidx.lifecycle.LifecycleOwner
import dev.agentring.app.Graph
import dev.agentring.core.Event
import dev.agentring.core.RelayApi
import dev.agentring.core.RelayException
import dev.agentring.core.RelaySocket
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.launch

/**
 * Keeps a live event stream open while the app is in the foreground. In the
 * background the phone relies on push (FCM) instead.
 */
class ConnectionManager : DefaultLifecycleObserver {
    private val _relayState = MutableStateFlow(RelaySocket.State.DISCONNECTED)
    val relayState: StateFlow<RelaySocket.State> = _relayState

    private val _agentOnline = MutableStateFlow<Boolean?>(null)
    val agentOnline: StateFlow<Boolean?> = _agentOnline

    @Volatile private var socket: RelaySocket? = null
    private var foreground = false

    override fun onStart(owner: LifecycleOwner) {
        synchronized(this) { foreground = true }
        connect()
    }

    override fun onStop(owner: LifecycleOwner) {
        synchronized(this) { foreground = false }
        disconnect()
    }

    @Synchronized
    fun connect() {
        if (!foreground || socket != null) return
        // Read before the pairing: an unpair in between then makes this work stale, never current.
        val generation = Graph.messages.generation
        val stale = { generation != Graph.messages.generation }
        val p = Graph.pairing.current ?: return
        if (Graph.pairing.revoked.value) return
        val api = RelayApi(p.relayUrl, p.deviceJwt, userAgent = Graph.userAgent)
        var created: RelaySocket? = null
        val s = RelaySocket(api, { Graph.messages.lastSeq }, object : RelaySocket.Listener {
            override fun onEvent(event: Event) {
                if (stale()) return
                if (event is Event.AgentPresence) _agentOnline.value = event.online
                when (event) {
                    // The socket may replay an old invite: check it's still ringing first.
                    is Event.CallInvite -> Graph.calls.onInvite(event, verifyFirst = true)
                    is Event.CallAnswer, is Event.CallEnd, is Event.CallMissed -> Graph.calls.onCallEvent(event)
                    else -> Unit
                }
                Graph.messages.apply(event, advanceSeq = true, generation = generation)
            }

            override fun onState(state: RelaySocket.State) {
                // A socket being torn down must not overwrite its replacement's state.
                if (socket === created) _relayState.value = state
            }

            override fun onUnauthorized() {
                Log.w(TAG, "relay rejected our credentials")
                if (!stale()) Graph.pairing.markRevoked()
            }

            override fun onWrongRelay(relayId: String) {
                Log.w(TAG, "disconnected: the server says it is relay $relayId, we are paired with ${p.relayId}")
            }
        }, expectedRelayId = p.relayId)
        created = s
        socket = s
        s.start()

        Graph.appScope.launch(Dispatchers.IO) {
            try {
                Graph.messages.refresh(api, generation)
                // A group call may be going on without this phone (missed ring, push lost): offer to join.
                val activeCall = api.status().activeCall
                if (!stale()) Graph.calls.onActiveCall(activeCall)
            } catch (e: RelayException) {
                if (e.isUnauthorized && !stale()) Graph.pairing.markRevoked()
            } catch (e: Exception) {
                Log.w(TAG, "refresh failed", e)
            }
            Graph.push.ensureRegistered()
        }
    }

    @Synchronized
    fun disconnect() {
        socket?.stop()
        socket = null
        _relayState.value = RelaySocket.State.DISCONNECTED
        _agentOnline.value = null
    }

    fun reconnect() {
        disconnect()
        connect()
    }

    private companion object {
        const val TAG = "ConnectionManager"
    }
}
