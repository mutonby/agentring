package dev.agentring.app.ui

import android.Manifest
import android.content.Context
import android.content.Intent
import android.media.AudioManager
import android.os.Build
import android.os.Bundle
import android.os.PowerManager
import android.os.SystemClock
import android.view.WindowManager
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.safeDrawingPadding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material3.FilledIconButton
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButtonDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableLongStateOf
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.lifecycleScope
import dev.agentring.app.Graph
import dev.agentring.app.R
import dev.agentring.app.call.CallInfo
import dev.agentring.app.call.CallPermissions
import dev.agentring.app.call.CallUi
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch

/**
 * Full-screen call UI. Shown over the lock screen (it's the notification's
 * full-screen intent) and opened by the Answer and Join actions, and by
 * "Call agent" once the call is placed ([outgoingIntent]).
 */
class CallActivity : ComponentActivity() {
    private var proximity: PowerManager.WakeLock? = null

    /** Runs once the microphone permission dialog is answered. */
    private var afterMic: (() -> Unit)? = null

    private val micPermission = registerForActivityResult(ActivityResultContracts.RequestPermission()) {
        // Go on either way: without the mic the user can still hear the agent.
        val action = afterMic
        afterMic = null
        action?.invoke()
        if (Graph.calls.state.value == null) finishAndRemoveTask()
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        if (Build.VERSION.SDK_INT >= 27) {
            setShowWhenLocked(true)
            setTurnScreenOn(true)
        } else {
            @Suppress("DEPRECATION")
            window.addFlags(WindowManager.LayoutParams.FLAG_SHOW_WHEN_LOCKED or WindowManager.LayoutParams.FLAG_TURN_SCREEN_ON)
        }
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        volumeControlStream = AudioManager.STREAM_VOICE_CALL
        enableEdgeToEdge()
        handle(intent)

        lifecycleScope.launch {
            Graph.calls.state.collect { s ->
                updateProximity(s)
                // Not while asking for the microphone before joining: there's no call state yet.
                if (s == null && afterMic == null) finishAndRemoveTask()
            }
        }
        setContent {
            AgentringTheme {
                val state by Graph.calls.state.collectAsStateWithLifecycle()
                state?.let {
                    CallScreen(
                        it,
                        onAnswer = ::answer,
                        onDecline = Graph.calls::decline,
                        onHangUp = Graph.calls::hangUp,
                        onMute = Graph.calls::toggleMute,
                        onSpeaker = Graph.calls::toggleSpeaker,
                    )
                }
            }
        }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        handle(intent)
    }

    private fun handle(intent: Intent?) {
        when (intent?.action) {
            ACTION_ANSWER -> answer()
            ACTION_JOIN -> infoFrom(intent)?.let { info -> withMic { Graph.calls.join(info) } }
        }
    }

    private fun answer() {
        if (Graph.calls.state.value !is CallUi.Incoming) return
        withMic { Graph.calls.answer() }
    }

    private fun withMic(action: () -> Unit) {
        if (CallPermissions.hasMic(this)) {
            action()
        } else {
            afterMic = action
            micPermission.launch(Manifest.permission.RECORD_AUDIO)
        }
    }

    /** Screen off when the phone is at the ear (earpiece only). */
    private fun updateProximity(s: CallUi?) {
        val want = s is CallUi.Active && !s.speaker || s is CallUi.Connecting || s is CallUi.Outgoing
        val pm = getSystemService(PowerManager::class.java)
        if (want && proximity == null && pm.isWakeLockLevelSupported(PowerManager.PROXIMITY_SCREEN_OFF_WAKE_LOCK)) {
            proximity = pm.newWakeLock(PowerManager.PROXIMITY_SCREEN_OFF_WAKE_LOCK, "agentring:proximity").apply { acquire(3 * 60 * 60 * 1000L) }
        } else if (!want) {
            proximity?.let { if (it.isHeld) it.release(PowerManager.RELEASE_FLAG_WAIT_FOR_NO_PROXIMITY) }
            proximity = null
        }
    }

    override fun onDestroy() {
        updateProximity(null)
        super.onDestroy()
    }

    companion object {
        const val ACTION_ANSWER = "dev.agentring.app.call.ANSWER"
        const val ACTION_JOIN = "dev.agentring.app.call.JOIN"
        private const val EXTRA_CALL_ID = "call_id"
        private const val EXTRA_AGENT_NAME = "agent_name"
        private const val EXTRA_REASON = "reason"
        private const val EXTRA_URGENT = "urgent"

        /** Opens the call screen for the call to the agent that [dev.agentring.app.call.CallManager.callAgent] placed. */
        fun outgoingIntent(context: Context): Intent =
            Intent(context, CallActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP)

        /** Opens the call screen and joins [info]'s call ("join the call"). */
        fun joinIntent(context: Context, info: CallInfo): Intent =
            Intent(context, CallActivity::class.java)
                .setAction(ACTION_JOIN)
                .putExtra(EXTRA_CALL_ID, info.callId)
                .putExtra(EXTRA_AGENT_NAME, info.agentName)
                .putExtra(EXTRA_REASON, info.reason)
                .putExtra(EXTRA_URGENT, info.urgent)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP)

        private fun infoFrom(intent: Intent): CallInfo? {
            val callId = intent.getStringExtra(EXTRA_CALL_ID) ?: return null
            return CallInfo(
                callId = callId,
                agentName = intent.getStringExtra(EXTRA_AGENT_NAME).orEmpty(),
                reason = intent.getStringExtra(EXTRA_REASON).orEmpty(),
                urgent = intent.getBooleanExtra(EXTRA_URGENT, false),
                expiresAt = 0,
            )
        }
    }
}

private val Green = Color(0xFF1E9E5A)
private val Red = Color(0xFFD93A3A)

@Composable
fun CallScreen(
    state: CallUi,
    onAnswer: () -> Unit,
    onDecline: () -> Unit,
    onHangUp: () -> Unit,
    onMute: () -> Unit,
    onSpeaker: () -> Unit,
) {
    val info = state.info
    Surface(
        modifier = Modifier
            .fillMaxSize()
            .background(Brush.radialGradient(listOf(Color(0xFF1C3A2C), Color(0xFF0C0F12)), radius = 1400f, center = androidx.compose.ui.geometry.Offset(540f, 0f))),
        color = Color.Transparent,
        contentColor = Color.White,
    ) {
        Column(
            Modifier.fillMaxSize().safeDrawingPadding().padding(horizontal = 32.dp, vertical = 48.dp),
            horizontalAlignment = Alignment.CenterHorizontally,
        ) {
            if (info.urgent) {
                Text(
                    stringResource(R.string.call_urgent),
                    color = Color.White,
                    fontWeight = FontWeight.Bold,
                    modifier = Modifier.background(Red, CircleShape).padding(horizontal = 14.dp, vertical = 4.dp),
                )
                Spacer(Modifier.height(16.dp))
            }
            Box(
                contentAlignment = Alignment.Center,
                modifier = Modifier.size(104.dp).background(Brush.linearGradient(listOf(Color(0xFF43D68F), Color(0xFF0B7A4B))), CircleShape),
            ) {
                Text(info.agentName.trim().take(1).uppercase().ifEmpty { "A" }, fontSize = 44.sp, fontWeight = FontWeight.SemiBold, color = Color.White)
            }
            Spacer(Modifier.height(20.dp))
            Text(info.agentName, fontSize = 34.sp, fontWeight = FontWeight.SemiBold, textAlign = TextAlign.Center, modifier = Modifier.testTag("call_agent"))
            Spacer(Modifier.height(10.dp))
            Text(statusText(state), fontSize = 18.sp, color = Color(0xFFB8C4CC), modifier = Modifier.testTag("call_status"))
            if (info.reason.isNotBlank()) {
                Spacer(Modifier.height(28.dp))
                Text(
                    info.reason,
                    fontSize = 20.sp,
                    textAlign = TextAlign.Center,
                    lineHeight = 28.sp,
                    modifier = Modifier.fillMaxWidth().testTag("call_reason"),
                )
            }
            Spacer(Modifier.weight(1f))
            when (state) {
                is CallUi.Incoming -> Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceEvenly) {
                    RoundButton(R.drawable.ic_call_end, stringResource(R.string.call_decline), Red, onDecline, "call_decline")
                    RoundButton(R.drawable.ic_call, stringResource(R.string.call_answer), Green, onAnswer, "call_answer")
                }
                is CallUi.Outgoing, is CallUi.Connecting, is CallUi.Active -> {
                    val active = state as? CallUi.Active
                    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceEvenly) {
                        ToggleButton(
                            if (active?.muted == true) R.drawable.ic_mic_off else R.drawable.ic_mic,
                            stringResource(R.string.call_mute),
                            on = active?.muted == true,
                            enabled = active != null,
                            onClick = onMute,
                            tag = "call_mute",
                        )
                        ToggleButton(
                            R.drawable.ic_volume_up,
                            stringResource(R.string.call_speaker),
                            on = active?.speaker == true,
                            enabled = active != null,
                            onClick = onSpeaker,
                            tag = "call_speaker",
                        )
                    }
                    Spacer(Modifier.height(40.dp))
                    RoundButton(R.drawable.ic_call_end, stringResource(R.string.call_hang_up), Red, onHangUp, "call_hang_up")
                }
                is CallUi.Ended -> Spacer(Modifier.height(96.dp))
            }
        }
    }
}

@Composable
private fun statusText(state: CallUi): String = when (state) {
    is CallUi.Incoming -> stringResource(R.string.call_incoming)
    is CallUi.Outgoing -> stringResource(R.string.call_calling, state.info.agentName)
    is CallUi.Connecting -> stringResource(R.string.call_connecting)
    is CallUi.Active -> if (state.reconnecting) stringResource(R.string.call_reconnecting) else elapsed(state.since)
    is CallUi.Ended -> stringResource(state.messageRes)
}

@Composable
private fun elapsed(since: Long): String {
    val now = remember { mutableLongStateOf(SystemClock.elapsedRealtime()) }
    LaunchedEffect(since) {
        while (true) {
            now.longValue = SystemClock.elapsedRealtime()
            delay(500)
        }
    }
    val s = ((now.longValue - since) / 1000).coerceAtLeast(0)
    return "%d:%02d".format(s / 60, s % 60)
}

@Composable
private fun RoundButton(icon: Int, label: String, color: Color, onClick: () -> Unit, tag: String) {
    Column(horizontalAlignment = Alignment.CenterHorizontally) {
        FilledIconButton(
            onClick = onClick,
            modifier = Modifier.size(76.dp).testTag(tag),
            colors = IconButtonDefaults.filledIconButtonColors(containerColor = color, contentColor = Color.White),
        ) {
            Icon(painterResource(icon), contentDescription = label, modifier = Modifier.size(34.dp))
        }
        Spacer(Modifier.height(8.dp))
        Text(label, fontSize = 14.sp)
    }
}

@Composable
private fun ToggleButton(icon: Int, label: String, on: Boolean, enabled: Boolean, onClick: () -> Unit, tag: String) {
    Column(horizontalAlignment = Alignment.CenterHorizontally) {
        FilledIconButton(
            onClick = onClick,
            enabled = enabled,
            modifier = Modifier.size(64.dp).testTag(tag),
            colors = IconButtonDefaults.filledIconButtonColors(
                containerColor = if (on) Color.White else Color(0x33FFFFFF),
                contentColor = if (on) Color(0xFF101418) else Color.White,
                disabledContainerColor = Color(0x1AFFFFFF),
                disabledContentColor = Color(0x66FFFFFF),
            ),
        ) {
            Icon(painterResource(icon), contentDescription = label, modifier = Modifier.size(28.dp))
        }
        Spacer(Modifier.height(8.dp))
        Text(label, fontSize = 14.sp)
    }
}
