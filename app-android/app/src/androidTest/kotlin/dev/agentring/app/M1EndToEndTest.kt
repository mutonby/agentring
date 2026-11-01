package dev.agentring.app

import android.Manifest
import android.app.NotificationManager
import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.Bundle
import androidx.compose.ui.test.hasTestTag
import androidx.compose.ui.test.hasText
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performScrollTo
import androidx.compose.ui.test.performTextInput
import androidx.core.app.RemoteInput
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import androidx.test.rule.GrantPermissionRule
import dev.agentring.app.notify.Notifications
import dev.agentring.app.push.PushHandler
import dev.agentring.app.ui.MainActivity
import dev.agentring.core.ProtocolJson
import dev.agentring.core.PushVerifier
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.BeforeClass
import org.junit.Rule
import org.junit.Test
import org.junit.rules.RuleChain
import org.junit.rules.TestRule
import org.junit.runner.RunWith

/**
 * M1 acceptance on a real emulator against a real relay (started by CI).
 * Instrumentation args: relayUrl (as seen from the emulator) and apiKey (agent key).
 */
@RunWith(AndroidJUnit4::class)
class M1EndToEndTest {
    companion object {
        private val args = InstrumentationRegistry.getArguments()
        private val relayUrl: String = args.getString("relayUrl") ?: "http://10.0.2.2:8080"
        private val apiKey: String = args.getString("apiKey") ?: "ak_ci_test_key"

        @BeforeClass
        @JvmStatic
        fun resetApp() {
            // Start from a fresh install state.
            Graph.unpair()
        }
    }

    private val compose = createAndroidComposeRule<MainActivity>()

    private val notificationPermission: TestRule =
        if (Build.VERSION.SDK_INT >= 33) GrantPermissionRule.grant(Manifest.permission.POST_NOTIFICATIONS)
        else TestRule { base, _ -> base }

    @get:Rule
    val rules: RuleChain = RuleChain.outerRule(notificationPermission).around(compose)

    private val http = OkHttpClient()
    private val context: Context get() = InstrumentationRegistry.getInstrumentation().targetContext

    private fun agent(method: String, path: String, json: String? = null): JsonObject {
        val req = Request.Builder().url(relayUrl + path)
            .header("Authorization", "Bearer $apiKey")
            .method(method, json?.toRequestBody("application/json".toMediaType()))
            .build()
        http.newCall(req).execute().use { res ->
            val body = res.body!!.string()
            assertTrue("$method $path -> ${res.code}: $body", res.isSuccessful)
            return if (body.isEmpty()) JsonObject(emptyMap()) else ProtocolJson.parseToJsonElement(body).jsonObject
        }
    }

    /** Runs a shell command as the shell user and waits for it to finish. */
    private fun shell(cmd: String) {
        val pfd = InstrumentationRegistry.getInstrumentation().uiAutomation.executeShellCommand(cmd)
        android.os.ParcelFileDescriptor.AutoCloseInputStream(pfd).use { it.readBytes() }
    }

    private fun screenshot(name: String) {
        compose.waitForIdle()
        // The shell user can write to /data/local/tmp; CI pulls these files.
        shell("screencap -p /data/local/tmp/agentring-$name.png")
    }

    private fun waitForTag(tag: String, text: String? = null, timeoutMs: Long = 15_000) {
        val matcher = if (text == null) hasTestTag(tag) else hasTestTag(tag).and(hasText(text, substring = true))
        compose.waitUntil(timeoutMs) { compose.onAllNodes(matcher).fetchSemanticsNodes().isNotEmpty() }
    }

    private fun waitForText(text: String, timeoutMs: Long = 15_000) {
        compose.waitUntil(timeoutMs) { compose.onAllNodes(hasText(text, substring = true)).fetchSemanticsNodes().isNotEmpty() }
    }

    private fun userMessages(): List<String> =
        agent("GET", "/v1/messages?from=user")["messages"]!!.jsonArray.map { it.jsonObject["text"]!!.jsonPrimitive.content }

    private fun waitFor(timeoutMs: Long = 15_000, what: String, cond: () -> Boolean) {
        val end = System.currentTimeMillis() + timeoutMs
        while (!cond()) {
            if (System.currentTimeMillis() > end) throw AssertionError("timed out waiting for $what")
            Thread.sleep(250)
        }
    }

    @Test
    fun pairMessageReplyAndPush() {
        // 1. Pair through the UI with a code from the relay (what `agentring pair` prints).
        val qr = agent("POST", "/v1/pairing-tokens", "{}")["qr_payload"]!!.jsonPrimitive.content
        compose.onNodeWithTag("get_started").performClick()
        // Setup tutorial: "cloud or own server" (only when the build links a cloud), then the pairing page.
        compose.waitUntil(15_000) {
            compose.onAllNodes(hasTestTag("setup_self").or(hasTestTag("show_paste"))).fetchSemanticsNodes().isNotEmpty()
        }
        if (compose.onAllNodes(hasTestTag("setup_self")).fetchSemanticsNodes().isNotEmpty()) {
            compose.onNodeWithTag("setup_self").performClick()
        }
        waitForTag("show_paste")
        compose.onNodeWithTag("show_paste").performClick()
        waitForTag("pairing_code")
        screenshot("01-pair")
        compose.onNodeWithTag("pairing_code").performScrollTo().performTextInput(qr)
        compose.onNodeWithTag("pair_button").performScrollTo().performClick()
        // Call readiness page, then "try it".
        waitForTag("calls_next", timeoutMs = 20_000)
        screenshot("02-calls")
        compose.onNodeWithTag("calls_next").performClick()
        waitForTag("continue")
        screenshot("02-paired")
        val pairing = assertNotNull_(Graph.pairing.current)
        compose.onNodeWithTag("continue").performClick()

        // 2. Home shows a live connection to the relay.
        waitForTag("relay_status", context.getString(R.string.connected), timeoutMs = 20_000)
        screenshot("03-home")

        // 3. Agent message arrives live in the thread (WebSocket path).
        compose.onNodeWithTag("tab_messages").performClick()
        val agentMsg = agent("POST", "/v1/messages", """{"text":"hola desde CI 👋"}""")
        val agentMsgId = agentMsg["msg_id"]!!.jsonPrimitive.content
        waitForText("hola desde CI")
        // Opening the thread sends a read receipt.
        waitFor(what = "read receipt") {
            agent("GET", "/v1/messages/$agentMsgId")["read_at"] != null
        }

        // 4. User replies from the app.
        compose.onNodeWithTag("message_input").performTextInput("respuesta desde la app")
        compose.onNodeWithTag("send_button").performClick()
        waitFor(what = "app reply on relay") { "respuesta desde la app" in userMessages() }
        screenshot("04-messages")

        // 5. Push path: a relay-signed payload (exactly what FCM delivers) shows a notification.
        compose.onNodeWithTag("tab_home").performClick()
        compose.waitForIdle()
        val pushed = agent("POST", "/v1/messages", """{"text":"¿Despliego a producción?"}""")
        val event = agent("GET", "/v1/events?after_seq=0&limit=500")["events"]!!.jsonArray
            .map { it.jsonObject }
            .first { it["msg_id"]?.jsonPrimitive?.content == pushed["msg_id"]!!.jsonPrimitive.content }
        val eventJson = event.toString()
        val data = mapOf(
            "v" to "1",
            "relay_id" to pairing.relayId,
            "event" to eventJson,
            "sig" to PushVerifier.sign(pairing.pushSecret, eventJson),
        )

        // A forged payload is rejected.
        val forged = data + ("sig" to PushVerifier.sign("AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA", eventJson))
        assertTrue(PushHandler.handle(context, forged) is PushHandler.Outcome.Ignored)

        val outcome = PushHandler.handle(context, data)
        assertTrue("outcome=$outcome", outcome is PushHandler.Outcome.Notified)
        val nm = context.getSystemService(NotificationManager::class.java)
        // NotificationManagerService enqueues asynchronously: the notification shows up in
        // activeNotifications a moment after notify() returns (slow on CI emulators).
        waitFor(timeoutMs = 5_000, what = "message notification posted") {
            nm.activeNotifications.any { it.id == Notifications.ID_MESSAGES }
        }
        val notification = nm.activeNotifications.firstOrNull { it.id == Notifications.ID_MESSAGES }
        shell("cmd statusbar expand-notifications")
        Thread.sleep(1500)
        shell("screencap -p /data/local/tmp/agentring-05-notification.png")
        shell("cmd statusbar collapse")

        // 6. Inline reply from the notification, through the real PendingIntent.
        val action = notification!!.notification.actions.first { it.remoteInputs?.isNotEmpty() == true }
        val fill = Intent()
        RemoteInput.addResultsToIntent(
            arrayOf(RemoteInput.Builder(Notifications.KEY_REPLY).build()),
            fill,
            Bundle().apply { putCharSequence(Notifications.KEY_REPLY, "sí, despliega") },
        )
        action.actionIntent.send(context, 0, fill)
        waitFor(what = "notification reply on relay") { "sí, despliega" in userMessages() }

        // 7. Status says the agent is offline (no daemon in this test) and the relay is connected.
        assertEquals(pairing.deviceId, agent("GET", "/v1/devices")["devices"]!!.jsonArray.last().jsonObject["device_id"]!!.jsonPrimitive.content)
    }

    private fun <T> assertNotNull_(v: T?): T {
        assertNotNull(v)
        return v!!
    }
}
