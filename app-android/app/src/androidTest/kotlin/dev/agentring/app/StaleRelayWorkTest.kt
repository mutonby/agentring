package dev.agentring.app

import androidx.lifecycle.ProcessLifecycleOwner
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import dev.agentring.app.data.Pairing
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File
import java.net.InetAddress
import java.net.ServerSocket
import java.net.Socket
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import kotlin.concurrent.thread

/**
 * Work started for one pairing must not reach the next one: a slow response from the
 * old relay arriving after unpair and re-pair must leave no trace in the message store,
 * the sync cursor, the join offer or the revoked flag. Uses two in-process fixture relays.
 */
@RunWith(AndroidJUnit4::class)
class StaleRelayWorkTest {
    private val context get() = InstrumentationRegistry.getInstrumentation().targetContext
    private val lifecycle get() = ProcessLifecycleOwner.get()

    /** A tiny HTTP server; `GET /v1/messages` may be held back until [release]. */
    private class FixtureRelay(
        private val messagesBody: () -> Pair<Int, String>,
        private val activeCall: String? = null,
        private val holdMessages: Boolean = false,
    ) {
        private val server = ServerSocket(0, 16, InetAddress.getByName("127.0.0.1"))
        val url = "http://127.0.0.1:${server.localPort}"
        val messagesAsked = CountDownLatch(1)
        val answered = CountDownLatch(1)
        val requests = CopyOnWriteArrayList<String>()
        private val gate = CountDownLatch(1)

        init {
            thread(isDaemon = true) {
                while (!server.isClosed) {
                    val s = runCatching { server.accept() }.getOrNull() ?: break
                    thread(isDaemon = true) { runCatching { serve(s) } }
                }
            }
        }

        fun release() = gate.countDown()

        private fun serve(s: Socket) = s.use {
            val reader = s.getInputStream().bufferedReader()
            val path = reader.readLine().split(" ")[1]
            while (reader.readLine().orEmpty().isNotEmpty()) Unit
            requests += path
            val (code, body) = when {
                path.startsWith("/v1/messages") -> {
                    messagesAsked.countDown()
                    if (holdMessages) gate.await(30, TimeUnit.SECONDS)
                    messagesBody().also { answered.countDown() }
                }
                path.startsWith("/v1/status") -> 200 to """{"relay_id":"rly_a","version":"0","agent_name":"A","agent_online":false,
                    "push_provider":"none"${activeCall?.let { ""","active_call":$it""" }.orEmpty()}}"""
                else -> 404 to """{"error":{"code":"not_found","message":"no"}}"""
            }
            val bytes = body.toByteArray()
            s.getOutputStream().apply {
                write("HTTP/1.1 $code X\r\nContent-Type: application/json\r\nContent-Length: ${bytes.size}\r\nConnection: close\r\n\r\n".toByteArray())
                write(bytes)
                flush()
            }
        }

        fun close() = server.close()
    }

    private fun pairing(url: String, id: String) = Pairing(
        relayUrl = url, relayId = "rly_$id", deviceId = "dev_$id", deviceJwt = "jwt_$id",
        pushSecret = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA", agentName = "Agent $id",
    )

    private fun page(id: String, text: String) =
        """{"messages":[{"msg_id":"$id","from":"agent","text":"$text","created_at":1}],"has_more":false}"""

    private fun waitFor(what: String, cond: () -> Boolean) {
        val end = System.currentTimeMillis() + 10_000
        while (!cond()) {
            assertTrue("timed out waiting for $what", System.currentTimeMillis() < end)
            Thread.sleep(50)
        }
    }

    @Before
    fun start() {
        // ProcessLifecycleOwner reports the previous test's activity stopping about 700 ms
        // later: let that pass, or it marks the connection as in the background mid-test.
        Thread.sleep(1_500)
        Graph.unpair()
        Graph.connection.onStart(lifecycle)
    }

    @After
    fun stop() {
        Graph.connection.onStop(lifecycle)
        Graph.unpair()
    }

    /** Pairs with [old], unpairs while its refresh is in flight, then pairs with [next]. */
    private fun rePairWhileHeld(old: FixtureRelay, next: FixtureRelay) {
        Graph.pairing.save(pairing(old.url, "a"))
        Graph.connection.reconnect()
        assertTrue("old relay never asked", old.messagesAsked.await(10, TimeUnit.SECONDS))
        Graph.unpair()
        Graph.pairing.save(pairing(next.url, "b"))
        Graph.connection.onStart(lifecycle)
        Graph.connection.reconnect()
        assertTrue("new relay never asked", next.messagesAsked.await(10, TimeUnit.SECONDS))
        waitFor("the new relay's message") { Graph.messages.messages.value.any { it.msgId == "msg_00000000000000000000000001" } }
        old.release()
        assertTrue(old.answered.await(10, TimeUnit.SECONDS))
        Thread.sleep(1_000) // Let the stale work finish.
    }

    @Test
    fun oldResponseDoesNotEnterTheNextPairing() {
        val call = """{"call_id":"call_old","direction":"agent_to_user","state":"active","reason":"old call","priority":"normal",
            "agent_name":"A","created_at":1,"answered_at":1}"""
        val old = FixtureRelay({ 200 to page("msg_ZZZZZZZZZZZZZZZZZZZZZZZZZZ", "from the old relay") }, activeCall = call, holdMessages = true)
        val next = FixtureRelay({ 200 to page("msg_00000000000000000000000001", "from the new relay") })
        try {
            rePairWhileHeld(old, next)
            val ids = Graph.messages.messages.value.map { it.msgId }
            assertEquals(listOf("msg_00000000000000000000000001"), ids)
            val cache = File(context.filesDir, "messages.json").readText()
            assertFalse("old message in the on-disk cache", "from the old relay" in cache)
            assertNull("old relay's call offered to join", Graph.calls.joinOffer.value)

            // The next refresh resumes from this relay's own cursor.
            Graph.messages.refresh(Graph.api()!!)
            assertTrue(next.requests.toString(), next.requests.last().contains("after=msg_00000000000000000000000001"))
        } finally {
            old.close()
            next.close()
        }
    }

    @Test
    fun oldUnauthorizedDoesNotRevokeTheNextPairing() {
        val old = FixtureRelay({ 401 to """{"error":{"code":"unauthorized","message":"revoked"}}""" }, holdMessages = true)
        val next = FixtureRelay({ 200 to page("msg_00000000000000000000000001", "from the new relay") })
        try {
            rePairWhileHeld(old, next)
            assertFalse("the new pairing was marked revoked by the old relay", Graph.pairing.revoked.value)
        } finally {
            old.close()
            next.close()
        }
    }
}
