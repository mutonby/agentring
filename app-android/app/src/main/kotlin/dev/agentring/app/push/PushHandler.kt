package dev.agentring.app.push

import android.content.Context
import android.util.Log
import dev.agentring.app.Graph
import dev.agentring.app.notify.Notifications
import dev.agentring.app.ui.AppVisibility
import dev.agentring.core.Event
import dev.agentring.core.PushVerifier

/** Handles one push payload. Shared by the FCM service and the instrumented tests. */
object PushHandler {
    sealed interface Outcome {
        data class Ignored(val reason: String) : Outcome
        data class Notified(val msgId: String) : Outcome
        data object Applied : Outcome
    }

    /** Runs on a background thread (network allowed). */
    fun handle(context: Context, data: Map<String, String>): Outcome {
        val generation = Graph.messages.generation
        val p = Graph.pairing.current ?: return Outcome.Ignored("not paired")
        val event = when (val r = PushVerifier.verify(data, p.relayId, p.pushSecret)) {
            is PushVerifier.Result.Rejected -> {
                Log.w(TAG, "rejected push: ${r.reason}")
                return Outcome.Ignored(r.reason)
            }
            is PushVerifier.Result.Ok -> r.event
        }
        return when (event) {
            is Event.MsgNew -> {
                var msg = event.toMessage()
                var truncated = event.truncated == true
                if (truncated) {
                    runCatching { Graph.api()?.getMessage(event.msgId) }.getOrNull()?.let {
                        msg = it
                        truncated = false
                    }
                }
                Graph.messages.upsert(msg, truncated = truncated, generation = generation)
                if (msg.fromAgent && !AppVisibility.messagesVisible) {
                    Notifications.showMessages(context, p.agentName)
                    Outcome.Notified(msg.msgId)
                } else {
                    Outcome.Applied
                }
            }
            is Event.CallInvite -> {
                Graph.calls.onInvite(event)
                Outcome.Applied
            }
            is Event.CallAnswer, is Event.CallEnd, is Event.CallMissed -> {
                Graph.calls.onCallEvent(event)
                Outcome.Applied
            }
            else -> {
                Graph.messages.apply(event, advanceSeq = false, generation = generation)
                Outcome.Applied
            }
        }
    }

    private const val TAG = "PushHandler"
}
