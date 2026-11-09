package dev.agentring.app.call

import android.content.Context
import android.content.Intent
import android.os.SystemClock
import android.telecom.DisconnectCause
import android.util.Log
import androidx.core.content.edit
import androidx.core.telecom.CallEndpointCompat
import dev.agentring.app.Graph
import dev.agentring.app.R
import dev.agentring.app.ui.CallActivity
import dev.agentring.core.Call
import dev.agentring.core.Event
import dev.agentring.core.JoinInfo
import dev.agentring.core.RelayException
import dev.agentring.core.RelayUrlPolicy
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.async
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.collect
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.flow.distinctUntilChanged
import kotlinx.coroutines.flow.stateIn
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/** What the call screen and the notification show. */
data class CallInfo(
    val callId: String,
    val agentName: String,
    val reason: String,
    val urgent: Boolean,
    val expiresAt: Long,
) {
    companion object {
        fun of(call: Call) = CallInfo(call.callId, call.agentName, call.reason.orEmpty(), call.priority == "urgent", expiresAt = 0)
    }
}

/** A group call from the agent in progress that this phone is not on: it can join. */
data class JoinOffer(
    val info: CallInfo,
    /** When the call was first answered (wall clock), for the notification's timer. */
    val answeredAt: Long?,
)

sealed interface CallUi {
    val info: CallInfo

    data class Incoming(override val info: CallInfo) : CallUi
    /** This phone called the agent: in the room, waiting for the agent to pick up. */
    data class Outgoing(override val info: CallInfo) : CallUi
    data class Connecting(override val info: CallInfo) : CallUi
    data class Active(
        override val info: CallInfo,
        /** [SystemClock.elapsedRealtime] when audio started, for the timer. */
        val since: Long,
        val muted: Boolean = false,
        val speaker: Boolean = false,
        val reconnecting: Boolean = false,
    ) : CallUi
    data class Ended(override val info: CallInfo, val messageRes: Int) : CallUi
}

/**
 * The one call: ringing, answering, calling the agent, the voice session and hanging up.
 * Invites arrive by FCM (app in background) or WebSocket (app open); both are
 * de-duplicated by call id. All state changes happen on the main thread.
 *
 * Group calls: while the agent's call is active and this phone is not on it (its
 * ring ended, it declined or hung up, or it never rang), [joinOffer] holds the
 * call and an ongoing "Join" notification is shown; [join] gets this phone in.
 *
 * Calls to the agent (M4, [callAgent]): the relay creates the call and returns
 * this phone's join info at once, so the phone joins the room while the call
 * rings ([CallUi.Outgoing]) and goes [CallUi.Active] when the agent accepts
 * (`call.accept` on the WebSocket, the agent showing up in the room, or a poll).
 */
class CallManager(private val context: Context) : TelecomBridge.Listener {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)
    private val ringer = Ringer(context)
    private val telecom = TelecomBridge(context, scope)
    private val prefs = context.getSharedPreferences("calls", Context.MODE_PRIVATE)

    private val _state = MutableStateFlow<CallUi?>(null)
    val state: StateFlow<CallUi?> = _state

    /** Calls we've already handled, so a second copy of an invite (FCM + WS) doesn't ring again. */
    private val seen = LinkedHashSet<String>()
    private var session: VoiceSession? = null
    private var telecomActive = false
    private var timeoutJob: Job? = null
    /** Collectors that live as long as the current call. */
    private val callJobs = mutableListOf<Job>()
    /** Outgoing call: this phone is in the LiveKit room. */
    private var inRoom = false

    private val _joinable = MutableStateFlow<JoinOffer?>(null)

    /** The call this phone may join, while it is not on a call itself. */
    val joinOffer: StateFlow<JoinOffer?> = combine(_joinable, _state) { j, s -> j.takeIf { s == null || s is CallUi.Ended } }
        .distinctUntilChanged()
        .stateIn(scope, SharingStarted.Eagerly, null)

    init {
        scope.launch {
            joinOffer.collect { j -> if (j != null) CallNotifications.showJoin(context, j) else CallNotifications.cancelJoin(context) }
        }
    }

    // ---------------------------------------------------------------- incoming

    /**
     * Rings for an invite. Safe to call from any thread; starts the foreground
     * service synchronously so it happens inside FCM's high-priority window.
     */
    fun onInvite(invite: Event.CallInvite, verifyFirst: Boolean = false) {
        if (invite.direction != "agent_to_user") return
        val now = System.currentTimeMillis()
        if (now > invite.expiresAt + CLOCK_SKEW_MS) {
            Log.i(TAG, "ignoring expired invite ${invite.callId}")
            return
        }
        val info = CallInfo(invite.callId, invite.agentName, invite.reason, invite.priority == "urgent", invite.expiresAt)
        synchronized(seen) {
            if (!seen.add(invite.callId)) return
            while (seen.size > 50) seen.remove(seen.first())
            // One call at a time: a new invite means any older call is over.
            if (_joinable.value?.info?.callId != invite.callId) _joinable.value = null
            val current = _state.value
            if (current != null && current !is CallUi.Ended) {
                Log.w(TAG, "already in a call; ignoring ${invite.callId}")
                return
            }
            if (!verifyFirst) {
                _state.value = CallUi.Incoming(info)
                CallService.startRinging(context)
            }
        }
        scope.launch {
            if (verifyFirst) {
                // A WebSocket replay may carry an invite that was answered or canceled since.
                val call = withContext(Dispatchers.IO) { runCatching { Graph.api()?.getCall(info.callId) }.getOrNull() }
                if (call?.isRinging != true || _state.value.let { it != null && it !is CallUi.Ended }) return@launch
                _state.value = CallUi.Incoming(info)
                CallService.startRinging(context)
            }
            startRinging(info)
        }
    }

    private suspend fun startRinging(info: CallInfo) {
        ringer.start(info.urgent)
        Log.i(TAG, "ringing ${info.callId} (${System.currentTimeMillis() - (info.expiresAt - RING_WINDOW_MS)} ms after invite)")
        scope.launch(Dispatchers.IO) {
            repeat(2) {
                try {
                    Graph.api()?.reportRinging(info.callId)
                    return@launch
                } catch (e: Exception) {
                    Log.w(TAG, "report ringing failed", e)
                }
            }
        }
        // Don't ring past the relay's deadline even if the "missed" push never arrives.
        timeoutJob = scope.launch {
            delay((info.expiresAt - System.currentTimeMillis() + 2_000).coerceAtLeast(0))
            if (isCurrent(info.callId) && _state.value is CallUi.Incoming) {
                finish(info, R.string.call_missed, DisconnectCause.MISSED)
                // Another phone may have answered: then offer to join instead of a missed call.
                val call = withContext(Dispatchers.IO) { runCatching { Graph.api()?.getCall(info.callId) }.getOrNull() }
                if (call?.isJoinable == true) offerJoin(call) else CallNotifications.showMissed(context, info)
            }
        }
        val onCall = { _state.value.let { it != null && it !is CallUi.Ended && it.info.callId == info.callId } }
        telecomActive = Graph.settings.useTelecom && telecom.addIncoming(info.agentName, info.callId, this, onCall)
        Log.i(TAG, "telecom: $telecomActive")
        if (!onCall()) return
        // Answered while Telecom was adding the call: `answer` couldn't tell it then.
        if (telecomActive && _state.value !is CallUi.Incoming) telecom.answer()
        // Build the WebRTC stack and warm the connection while the user decides.
        val s = VoiceSession(context, scope, telecomRoutesAudio = telecomActive)
        session = s
        prefs.getString(KEY_LAST_LIVEKIT_URL, null)?.let { s.prepare(it) }
    }

    /** Answers [callId] if it is the call ringing now; a stale action for an earlier call does nothing. */
    fun answer(callId: String) {
        val cur = _state.value as? CallUi.Incoming ?: return
        val info = cur.info
        if (info.callId != callId) {
            Log.w(TAG, "ignoring answer for $callId: ${info.callId} is ringing")
            return
        }
        ringer.stop()
        timeoutJob?.cancel()
        _state.value = CallUi.Connecting(info)
        if (telecomActive) telecom.answer()
        CallService.promoteToActive(context)
        scope.launch {
            val join = try {
                withContext(Dispatchers.IO) { acceptOrJoin(info.callId) }
            } catch (e: RelayException) {
                Log.w(TAG, "accept failed: ${e.code}")
                finish(info, if (e.code == "invalid_state" || e.code == "call_ended") R.string.call_unavailable else R.string.call_failed, DisconnectCause.ERROR)
                return@launch
            } catch (e: Exception) {
                Log.w(TAG, "accept failed", e)
                finish(info, R.string.call_failed, DisconnectCause.ERROR)
                return@launch
            }
            connect(info, join)
        }
    }

    /** Blocking. Accept while ringing; if the ring window closed meanwhile, join the call instead. */
    private fun acceptOrJoin(callId: String): JoinInfo {
        val api = Graph.api()!!
        return try {
            api.acceptCall(callId).join
        } catch (e: RelayException) {
            if (e.code != "invalid_state") throw e
            api.joinCall(callId).join
        }
    }

    /**
     * "Join the call": gets this phone into an agent call in progress that it is
     * not on. Called from the Join notification / button (the app is in the
     * foreground, so the microphone foreground service is allowed).
     */
    fun join(info: CallInfo) {
        val cur = _state.value
        if (cur != null && cur !is CallUi.Ended) {
            if (cur is CallUi.Incoming) answer(info.callId)
            return
        }
        synchronized(seen) { seen.add(info.callId) } // a replayed invite must not ring now
        _joinable.value = null
        _state.value = CallUi.Connecting(info)
        CallService.promoteToActive(context)
        scope.launch {
            val request = async(Dispatchers.IO) { runCatching { Graph.api()!!.joinCall(info.callId).join } }
            val stillJoining = { isCurrent(info.callId) && _state.value is CallUi.Connecting }
            val added = Graph.settings.useTelecom && telecom.addOutgoing(info.agentName, info.callId, this@CallManager)
            if (added && !stillJoining()) {
                // Hung up while Telecom was adding the call: don't leave a ghost call behind.
                telecom.disconnect(DisconnectCause.LOCAL)
            } else if (added) {
                telecomActive = true
                telecom.activate()
            }
            Log.i(TAG, "joining ${info.callId}, telecom: $telecomActive")
            val join = request.await().getOrElse { e ->
                Log.w(TAG, "join failed", e)
                val over = e is RelayException && e.code == "call_ended"
                finish(info, if (over) R.string.call_unavailable else R.string.call_failed, DisconnectCause.ERROR)
                if (!over) recheckJoinable(info.callId)
                return@launch
            }
            if (!stillJoining()) {
                // Hung up before the relay answered: leave again so the relay doesn't count us in.
                withContext(Dispatchers.IO) { runCatching { Graph.api()?.endCall(info.callId) }.getOrNull() }?.let(::offerJoin)
                return@launch
            }
            connect(info, join)
        }
    }

    /** Joins the LiveKit room with a fresh token, for an answered or joined call. */
    private suspend fun connect(info: CallInfo, join: JoinInfo) {
        try {
            RelayUrlPolicy.checkLiveKit(join.livekitUrl)
        } catch (e: IllegalArgumentException) {
            Log.e(TAG, "refusing LiveKit URL from relay: ${e.message}")
            withContext(Dispatchers.IO) { runCatching { Graph.api()?.endCall(info.callId) } }
            finish(info, R.string.call_failed, DisconnectCause.ERROR)
            return
        }
        prefs.edit { putString(KEY_LAST_LIVEKIT_URL, join.livekitUrl) }
        val s = session ?: VoiceSession(context, scope, telecomActive).also { session = it }
        watch(info, s)
        val t0 = SystemClock.elapsedRealtime()
        try {
            s.connect(join.livekitUrl, join.livekitToken, withMic = CallPermissions.hasMic(context))
        } catch (e: Exception) {
            Log.w(TAG, "LiveKit connect failed", e)
            val call = withContext(Dispatchers.IO) { runCatching { Graph.api()?.endCall(info.callId) }.getOrNull() }
            finish(info, R.string.call_failed, DisconnectCause.ERROR)
            call?.let(::offerJoin)
            return
        }
        Log.i(TAG, "in the room after ${SystemClock.elapsedRealtime() - t0} ms")
        if (!isCurrent(info.callId)) return
        inRoom = true
        when (_state.value) {
            is CallUi.Connecting -> goActive(info)
            // Calling the agent: stay "Calling…" until it picks up, unless it already did.
            is CallUi.Outgoing -> if (s.agentInRoom.value) onAgentAnswered(info.callId)
            else -> Unit
        }
    }

    private fun goActive(info: CallInfo) {
        _state.value = CallUi.Active(info, since = SystemClock.elapsedRealtime(), speaker = speakerOn())
        CallService.refresh()
    }

    // ---------------------------------------------------------------- outgoing (M4)

    /**
     * Calls the agent. Must be called with the app in the foreground (it starts the
     * microphone foreground service). Returns null once the call is ringing (open
     * [CallActivity]), or a message to show when it could not be placed.
     */
    suspend fun callAgent(): Int? {
        val cur = _state.value
        if (cur != null && cur !is CallUi.Ended) return if (cur is CallUi.Outgoing || cur is CallUi.Active) null else R.string.call_agent_busy
        val created = try {
            withContext(Dispatchers.IO) { Graph.api()!!.createCall() }
        } catch (e: RelayException) {
            Log.w(TAG, "create call failed: ${e.status} ${e.code}")
            return when (e.code) {
                "agent_offline" -> R.string.call_agent_offline
                "busy" -> R.string.call_agent_busy
                "quota_exceeded" -> R.string.call_agent_quota
                "calls_unavailable" -> R.string.call_agent_unavailable
                else -> R.string.call_failed
            }
        } catch (e: Exception) {
            Log.w(TAG, "create call failed", e)
            return R.string.call_failed
        }
        val call = created.call
        val info = CallInfo.of(call)
        synchronized(seen) { seen.add(call.callId) }
        _state.value.let {
            if (it != null && it !is CallUi.Ended) {
                // An agent call came in meanwhile: give up ours.
                withContext(Dispatchers.IO) { runCatching { Graph.api()?.endCall(call.callId) } }
                return R.string.call_agent_busy
            }
        }
        _joinable.value = null
        inRoom = false
        _state.value = CallUi.Outgoing(info)
        CallService.promoteToActive(context)
        Log.i(TAG, "calling the agent: ${call.callId}")
        scope.launch {
            val stillCalling = { isCurrent(info.callId) && _state.value !is CallUi.Ended }
            val added = Graph.settings.useTelecom && telecom.addOutgoing(info.agentName, info.callId, this@CallManager)
            if (!stillCalling()) {
                if (added) telecom.disconnect(DisconnectCause.LOCAL) // hung up while Telecom was adding the call
                return@launch
            }
            telecomActive = added
            // The agent may have answered while Telecom was adding the call.
            if (added && _state.value !is CallUi.Outgoing) telecom.activate()
            Log.i(TAG, "telecom: $telecomActive")
            connect(info, created.join)
        }
        callJobs += scope.launch { pollOutgoing(info) }
        return null
    }

    /**
     * Backup for the WebSocket while calling: the relay doesn't push events of
     * calls to the agent, and the socket may be down (screen off, network change).
     */
    private suspend fun pollOutgoing(info: CallInfo) {
        val deadline = SystemClock.elapsedRealtime() + OUTGOING_GIVE_UP_MS
        while (isCurrent(info.callId) && _state.value is CallUi.Outgoing) {
            delay(OUTGOING_POLL_MS)
            if (SystemClock.elapsedRealtime() > deadline) {
                // The relay's ring timer should have ended it long ago; don't ring forever.
                withContext(Dispatchers.IO) { runCatching { Graph.api()?.endCall(info.callId) } }
                finish(info, R.string.call_agent_no_answer, DisconnectCause.MISSED)
                return
            }
            val call = withContext(Dispatchers.IO) { runCatching { Graph.api()?.getCall(info.callId) }.getOrNull() } ?: continue
            onOutgoingState(info.callId, call.state)
        }
    }

    /** The relay's state of the call we placed. */
    private fun onOutgoingState(callId: String, state: String) {
        val cur = _state.value
        if (cur?.info?.callId != callId || cur is CallUi.Ended) return
        when (state) {
            "active" -> onAgentAnswered(callId)
            "declined" -> finish(cur.info, R.string.call_agent_declined, DisconnectCause.REJECTED)
            "missed" -> finish(cur.info, R.string.call_agent_no_answer, DisconnectCause.MISSED)
            "canceled", "ended" -> finish(cur.info, R.string.call_ended, DisconnectCause.REMOTE)
        }
    }

    private fun onAgentAnswered(callId: String) {
        val cur = _state.value as? CallUi.Outgoing ?: return
        if (cur.info.callId != callId) return
        Log.i(TAG, "the agent answered $callId")
        if (telecomActive) telecom.activate()
        // Not in the room yet: connect() finishes the switch to Active.
        if (inRoom) goActive(cur.info) else _state.value = CallUi.Connecting(cur.info)
    }

    fun decline() {
        val cur = _state.value as? CallUi.Incoming ?: return
        // If another phone is already on the call, this one can still join it later.
        scope.launch {
            withContext(Dispatchers.IO) { runCatching { Graph.api()?.declineCall(cur.info.callId) }.getOrNull() }?.let(::offerJoin)
        }
        finish(cur.info, R.string.call_declined, DisconnectCause.REJECTED)
    }

    fun hangUp() {
        val cur = _state.value ?: return
        when (cur) {
            is CallUi.Incoming -> decline()
            // While calling the agent this cancels the call (relay: `canceled`).
            is CallUi.Outgoing, is CallUi.Connecting, is CallUi.Active -> {
                // Leaving a group call: the others may still be on it, so this phone can rejoin.
                scope.launch {
                    withContext(Dispatchers.IO) { runCatching { Graph.api()?.endCall(cur.info.callId) }.getOrNull() }?.let(::offerJoin)
                }
                finish(cur.info, R.string.call_ended, DisconnectCause.LOCAL)
            }
            is CallUi.Ended -> Unit
        }
    }

    fun toggleMute() {
        val cur = _state.value as? CallUi.Active ?: return
        session?.setMuted(!cur.muted)
        _state.value = cur.copy(muted = !cur.muted)
    }

    fun toggleSpeaker() {
        val cur = _state.value as? CallUi.Active ?: return
        val on = !cur.speaker
        if (telecomActive) {
            val eps = telecom.endpoints.value
            val target = if (on) {
                eps.firstOrNull { it.type == CallEndpointCompat.TYPE_SPEAKER }
            } else {
                eps.firstOrNull { it.type == CallEndpointCompat.TYPE_BLUETOOTH }
                    ?: eps.firstOrNull { it.type == CallEndpointCompat.TYPE_WIRED_HEADSET }
                    ?: eps.firstOrNull { it.type == CallEndpointCompat.TYPE_EARPIECE }
            }
            target?.let { telecom.route(it) }
        } else {
            session?.setSpeaker(on)
        }
        _state.value = cur.copy(speaker = on)
    }

    private fun speakerOn(): Boolean =
        if (telecomActive) telecom.endpoint.value?.type == CallEndpointCompat.TYPE_SPEAKER else session?.speakerOn == true

    /** Relay events (push or WebSocket) about a call we may be showing or may join. */
    fun onCallEvent(event: Event) {
        scope.launch {
            when (event) {
                is Event.CallEnd -> if (_joinable.value?.info?.callId == event.callId) _joinable.value = null
                is Event.CallMissed -> if (_joinable.value?.info?.callId == event.callId) _joinable.value = null
                // Another phone answered or joined an agent call: offer to join it if we're not on it.
                is Event.CallAnswer -> if (event.type == "call.accept" && event.by == "user" &&
                    event.deviceId != Graph.pairing.current?.deviceId &&
                    _state.value.let { it == null || it is CallUi.Ended || it.info.callId != event.callId } &&
                    _joinable.value?.info?.callId != event.callId
                ) {
                    recheckJoinable(event.callId)
                }
                else -> Unit
            }
            val cur = _state.value ?: return@launch
            val info = cur.info
            if (cur is CallUi.Outgoing) {
                if (event is Event.CallAnswer && event.callId == info.callId) {
                    onOutgoingState(info.callId, if (event.type == "call.accept") "active" else "declined")
                } else if (event is Event.CallMissed && event.callId == info.callId) {
                    onOutgoingState(info.callId, "missed")
                } else if (event is Event.CallEnd && event.callId == info.callId) {
                    onOutgoingState(info.callId, "ended")
                }
                return@launch
            }
            when (event) {
                // Group calls: another phone answering or declining doesn't stop ours
                // ringing; we can still join until the call stops ringing.
                is Event.CallEnd -> if (event.callId == info.callId) {
                    val quota = event.reason == "quota" // the relay cut the call: the month's minutes ran out
                    if (cur is CallUi.Ended) {
                        // The voice session may notice first ("ended by the agent"): say why instead.
                        if (quota) _state.value = cur.copy(messageRes = R.string.call_ended_quota)
                    } else {
                        val missed = cur is CallUi.Incoming
                        finish(info, if (missed) R.string.call_missed else if (quota) R.string.call_ended_quota else R.string.call_ended_by_agent,
                            if (missed) DisconnectCause.CANCELED else DisconnectCause.REMOTE, notifyMissed = missed)
                    }
                }
                is Event.CallMissed -> if (event.callId == info.callId && cur is CallUi.Incoming) {
                    finish(info, R.string.call_missed, DisconnectCause.MISSED, notifyMissed = true)
                }
                else -> Unit
            }
        }
    }

    private fun watch(info: CallInfo, s: VoiceSession) {
        callJobs += scope.launch {
            s.signals.collect { sig ->
                val cur = _state.value as? CallUi.Active
                when (sig) {
                    VoiceSession.Signal.Reconnecting -> if (cur != null) _state.value = cur.copy(reconnecting = true)
                    VoiceSession.Signal.Reconnected -> if (cur != null) _state.value = cur.copy(reconnecting = false)
                    is VoiceSession.Signal.Gone -> if (isCurrent(info.callId) && _state.value !is CallUi.Ended) {
                        Log.i(TAG, "voice session gone: ${sig.reason}")
                        val calling = _state.value is CallUi.Outgoing
                        scope.launch {
                            // Lost the connection while others are still talking: offer to rejoin.
                            withContext(Dispatchers.IO) { runCatching { Graph.api()?.endCall(info.callId) }.getOrNull() }?.let(::offerJoin)
                        }
                        finish(info, if (calling) R.string.call_failed else R.string.call_ended_by_agent, DisconnectCause.REMOTE)
                    }
                    null -> Unit
                }
            }
        }
        callJobs += scope.launch {
            s.agentInRoom.collect { here -> if (here && inRoom) onAgentAnswered(info.callId) }
        }
        callJobs += scope.launch {
            // Muted or unmuted outside the app (Android Auto, a watch): follow it.
            telecom.systemMuted.collect { muted ->
                val cur = _state.value as? CallUi.Active ?: return@collect
                if (telecomActive && cur.muted != muted) {
                    session?.setMuted(muted)
                    _state.value = cur.copy(muted = muted)
                }
            }
        }
        callJobs += scope.launch {
            telecom.endpoint.collect { ep ->
                val cur = _state.value as? CallUi.Active ?: return@collect
                if (ep != null) _state.value = cur.copy(speaker = ep.type == CallEndpointCompat.TYPE_SPEAKER)
            }
        }
    }

    // ---------------------------------------------------------------- join offer

    /** Offers "Join" for [call] if it is an agent call in progress and this phone is not on it. */
    private fun offerJoin(call: Call) {
        if (!call.isJoinable) {
            if (_joinable.value?.info?.callId == call.callId) _joinable.value = null
            return
        }
        val cur = _state.value
        if (cur != null && cur !is CallUi.Ended && cur.info.callId == call.callId) return
        _joinable.value = JoinOffer(CallInfo.of(call), call.answeredAt)
    }

    /** Asks the relay whether [callId] can be joined (events may be stale or replayed). */
    private fun recheckJoinable(callId: String) {
        scope.launch {
            val call = withContext(Dispatchers.IO) { runCatching { Graph.api()?.getCall(callId) }.getOrNull() } ?: return@launch
            offerJoin(call)
        }
    }

    /** The relay's view of the open call (`active_call` in /v1/status), e.g. when the app comes to the foreground. */
    fun onActiveCall(call: Call?) {
        scope.launch {
            if (call == null) _joinable.value = null else offerJoin(call)
        }
    }

    // ---------------------------------------------------------------- Telecom callbacks

    override fun onSystemAnswer(callId: String) {
        scope.launch {
            answer(callId)
            // Answered from a headset/watch: bring up the call screen if Android lets us.
            runCatching {
                context.startActivity(Intent(context, CallActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
            }
        }
    }

    override fun onSystemDisconnect(cause: DisconnectCause) {
        scope.launch {
            telecomActive = false
            when (_state.value) {
                is CallUi.Incoming -> decline()
                is CallUi.Outgoing, is CallUi.Connecting, is CallUi.Active -> hangUp()
                else -> Unit
            }
        }
    }

    // ---------------------------------------------------------------- teardown

    private fun isCurrent(callId: String) = _state.value?.info?.callId == callId

    private fun finish(info: CallInfo, messageRes: Int, cause: Int, notifyMissed: Boolean = false) {
        if (!isCurrent(info.callId) || _state.value is CallUi.Ended) return
        Log.i(TAG, "call ${info.callId} finished: ${context.getString(messageRes)}")
        ringer.stop()
        timeoutJob?.cancel()
        callJobs.forEach { it.cancel() }
        callJobs.clear()
        session?.close()
        session = null
        inRoom = false
        if (telecomActive) telecom.disconnect(cause)
        telecomActive = false
        CallService.stop()
        if (notifyMissed) CallNotifications.showMissed(context, info)
        _state.value = CallUi.Ended(info, messageRes)
        scope.launch {
            delay(ENDED_LINGER_MS)
            if (_state.value.let { it is CallUi.Ended && it.info.callId == info.callId }) _state.value = null
        }
    }

    companion object {
        private const val TAG = "CallManager"
        private const val KEY_LAST_LIVEKIT_URL = "last_livekit_url"
        private const val CLOCK_SKEW_MS = 5_000L
        private const val RING_WINDOW_MS = 30_000L
        const val ENDED_LINGER_MS = 2_000L
        private const val OUTGOING_POLL_MS = 2_000L
        /** Well past the relay's ring timeout (30 s by default). */
        private const val OUTGOING_GIVE_UP_MS = 75_000L
    }
}
