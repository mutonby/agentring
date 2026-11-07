package dev.agentring.app

import android.Manifest
import android.app.Notification
import android.app.NotificationManager
import android.os.Build
import android.os.ParcelFileDescriptor
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import androidx.test.rule.GrantPermissionRule
import dev.agentring.app.call.CallInfo
import dev.agentring.app.call.CallNotifications
import dev.agentring.app.call.CallUi
import dev.agentring.app.call.JoinOffer
import dev.agentring.app.call.LockScreenPrivacy
import dev.agentring.app.call.Ringer
import dev.agentring.app.notify.Notifications
import dev.agentring.core.Event
import dev.agentring.core.Message
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TestRule
import org.junit.runner.RunWith

/** What the app gives away, or lets through, while the phone is locked. No relay needed. */
@RunWith(AndroidJUnit4::class)
class LockScreenPolicyTest {
    @get:Rule
    val notificationPermission: TestRule =
        if (Build.VERSION.SDK_INT >= 33) GrantPermissionRule.grant(Manifest.permission.POST_NOTIFICATIONS)
        else TestRule { base, _ -> base }

    private val context get() = InstrumentationRegistry.getInstrumentation().targetContext
    private val reason = "the private reason"
    private val call = CallInfo("call_1", "Agent", reason, urgent = false, expiresAt = System.currentTimeMillis() + 30_000)

    private fun shell(cmd: String): String {
        val pfd = InstrumentationRegistry.getInstrumentation().uiAutomation.executeShellCommand(cmd)
        return ParcelFileDescriptor.AutoCloseInputStream(pfd).use { it.readBytes().decodeToString() }.trim()
    }

    private fun Notification.text() = extras.getCharSequence(Notification.EXTRA_TEXT)?.toString().orEmpty()
    private fun Notification.title() = extras.getCharSequence(Notification.EXTRA_TITLE)?.toString().orEmpty()

    private fun waitFor(id: Int): Notification {
        val nm = context.getSystemService(NotificationManager::class.java)
        val end = System.currentTimeMillis() + 5_000
        while (true) {
            nm.activeNotifications.firstOrNull { it.id == id }?.let { return it.notification }
            assertTrue("notification $id not posted", System.currentTimeMillis() < end)
            Thread.sleep(50)
        }
    }

    @Test
    fun incomingCallKeepsTheReasonOutOfThePublicVersion() {
        val n = CallNotifications.incoming(context, call)
        assertEquals(reason, n.text())
        assertEquals(Notification.VISIBILITY_PRIVATE, n.visibility)
        val public = n.publicVersion!!
        assertEquals("Agent", public.title())
        assertFalse(public.text(), reason in public.text())
        assertEquals("Answer and Decline stay on the lock screen", n.actions.size, public.actions.size)
    }

    @Test
    fun joinNotificationKeepsTheReasonOutOfThePublicVersion() {
        CallNotifications.showJoin(context, JoinOffer(call, answeredAt = null))
        try {
            val n = waitFor(2003)
            assertEquals(Notification.VISIBILITY_PRIVATE, n.visibility)
            assertFalse(reason in n.publicVersion!!.text())
            assertEquals(1, n.publicVersion.actions.size)
        } finally {
            CallNotifications.cancelJoin(context)
        }
    }

    @Test
    fun followsTheLockScreenSetting() {
        val key = "lock_screen_allow_private_notifications"
        val before = shell("settings get secure $key")
        try {
            shell("settings put secure $key 1")
            assertFalse("shown by default", LockScreenPrivacy.hidesSensitiveContent(context))
            shell("settings put secure $key 0")
            assertTrue(LockScreenPrivacy.hidesSensitiveContent(context))
        } finally {
            shell(if (before == "null") "settings delete secure $key" else "settings put secure $key $before")
        }
    }

    @Test
    fun inlineReplyAsksForAnUnlock() {
        if (Build.VERSION.SDK_INT < 31) return
        val message = Message("msg_1", Message.FROM_AGENT, "hello", createdAt = System.currentTimeMillis())
        Notifications.showMessages(context, "Agent", listOf(message))
        try {
            val reply = waitFor(Notifications.ID_MESSAGES).actions.first { it.remoteInputs?.isNotEmpty() == true }
            assertTrue(reply.isAuthenticationRequired)
        } finally {
            Notifications.cancelMessages(context)
        }
    }

    @Test
    fun urgentUsesTheAlarmStreamOnlyWithoutDndOrWithTheGrant() {
        val all = NotificationManager.INTERRUPTION_FILTER_ALL
        val dnd = listOf(
            NotificationManager.INTERRUPTION_FILTER_PRIORITY,
            NotificationManager.INTERRUPTION_FILTER_ALARMS,
            NotificationManager.INTERRUPTION_FILTER_NONE,
        )
        assertTrue(Ringer.alarmStreamAllowed(all, dndAccess = false))
        assertTrue(Ringer.alarmStreamAllowed(all, dndAccess = true))
        dnd.forEach {
            assertFalse("filter $it", Ringer.alarmStreamAllowed(it, dndAccess = false))
            assertTrue("filter $it", Ringer.alarmStreamAllowed(it, dndAccess = true))
        }
    }

    @Test
    fun answerOnlyTouchesItsOwnCall() {
        val now = System.currentTimeMillis()
        val invite = Event.CallInvite("call.invite", "evt_1", now, null, "call_ringing_now", "agent_to_user", "Agent", reason, "normal", now + 30_000)
        val calls = Graph.calls
        // Hanging up at once would race Telecom adding the call: this test is not about Telecom.
        Graph.settings.useTelecom = false
        calls.onInvite(invite)
        try {
            val ringing = { (calls.state.value as? CallUi.Incoming)?.info?.callId }
            assertEquals("call_ringing_now", ringing())
            InstrumentationRegistry.getInstrumentation().runOnMainSync { calls.answer("call_from_before") }
            assertEquals("a stale answer changed the call", "call_ringing_now", ringing())
        } finally {
            InstrumentationRegistry.getInstrumentation().runOnMainSync { calls.hangUp() }
            Graph.settings.useTelecom = true
        }
    }
}
