package dev.agentring.core

import okhttp3.OkHttpClient
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import java.util.concurrent.Executors
import java.util.concurrent.RejectedExecutionException
import java.util.concurrent.ScheduledFuture
import java.util.concurrent.TimeUnit
import kotlin.math.min
import kotlin.random.Random

/**
 * Resumable event stream: reconnects with exponential backoff and resumes
 * from the last `seq` the caller has processed. With [expectedRelayId], a server whose
 * `ws.hello` names another relay is dropped and nothing it sends is delivered.
 */
class RelaySocket(
    private val api: RelayApi,
    private val lastSeq: () -> Long,
    private val listener: Listener,
    private val http: OkHttpClient = RelayApi.defaultClient,
    private val expectedRelayId: String? = null,
) {
    enum class State { CONNECTING, CONNECTED, DISCONNECTED }

    interface Listener {
        fun onEvent(event: Event)
        fun onState(state: State)
        /** Credentials rejected (revoked device). The socket stops reconnecting. */
        fun onUnauthorized()
        /** The server is not the relay we paired with. The socket stops reconnecting. */
        fun onWrongRelay(relayId: String) = Unit
    }

    private val scheduler = Executors.newSingleThreadScheduledExecutor { r -> Thread(r, "relay-socket").apply { isDaemon = true } }
    private var ws: WebSocket? = null
    private var retry: ScheduledFuture<*>? = null
    private var attempts = 0
    @Volatile private var running = false
    @Volatile private var wrongRelay = false

    /** Runs on the socket's thread; silently dropped once the socket is stopped. */
    private fun post(block: () -> Unit) {
        try {
            scheduler.execute(block)
        } catch (e: RejectedExecutionException) {
            // stopped
        }
    }

    fun start() = post {
        if (running) return@post
        running = true
        connect()
    }

    /** Closes the connection for good and releases the thread. A stopped socket can't be restarted. */
    fun stop() = post {
        running = false
        retry?.cancel(false)
        ws?.close(1000, "bye")
        ws = null
        listener.onState(State.DISCONNECTED)
        scheduler.shutdown()
    }

    private fun connect() {
        if (!running) return
        listener.onState(State.CONNECTING)
        val socket = http.newWebSocket(api.webSocketRequest(lastSeq()), object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) = post {
                if (ws !== webSocket) return@post
                attempts = 0
                listener.onState(State.CONNECTED)
            }

            override fun onMessage(webSocket: WebSocket, text: String) {
                val event = try {
                    Event.parse(text)
                } catch (e: Exception) {
                    return
                }
                if (wrongRelay) return
                if (event is Event.WsHello && expectedRelayId != null && event.relayId != expectedRelayId) {
                    wrongRelay = true
                    post { onWrongRelay(webSocket, event.relayId) }
                    return
                }
                listener.onEvent(event)
            }

            override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                webSocket.close(1000, null)
                post { onDown(webSocket, unauthorized = code == 4401) }
            }

            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) =
                post { onDown(webSocket, unauthorized = response?.code == 401) }
        })
        ws = socket
    }

    private fun onWrongRelay(socket: WebSocket, relayId: String) {
        if (ws !== socket) return
        running = false
        ws = null
        socket.close(1000, "wrong relay")
        listener.onState(State.DISCONNECTED)
        listener.onWrongRelay(relayId)
    }

    private fun onDown(socket: WebSocket, unauthorized: Boolean) {
        if (ws !== socket) return
        ws = null
        listener.onState(State.DISCONNECTED)
        if (unauthorized) {
            running = false
            listener.onUnauthorized()
            return
        }
        if (!running) return
        val base = min(30_000L, 1_000L shl min(attempts, 5))
        attempts++
        val delay = (base * (0.8 + Random.nextDouble() * 0.4)).toLong()
        retry = scheduler.schedule({ connect() }, delay, TimeUnit.MILLISECONDS)
    }
}
