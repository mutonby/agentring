package dev.agentring.app

import android.app.Application
import android.os.Build
import dev.agentring.app.call.CallManager
import dev.agentring.app.data.AppSettings
import dev.agentring.app.data.MessageRepository
import dev.agentring.app.data.Pairing
import dev.agentring.app.data.PairingStore
import dev.agentring.app.net.ConnectionManager
import dev.agentring.app.notify.Notifications
import dev.agentring.app.push.PushRegistrar
import dev.agentring.core.PairRequest
import dev.agentring.core.QrPayload
import dev.agentring.core.RelayApi
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch

/** Process-wide singletons (tiny manual DI). */
object Graph {
    lateinit var app: Application
        private set

    val appScope = CoroutineScope(SupervisorJob() + Dispatchers.Default)

    val pairing: PairingStore by lazy { PairingStore(app) }
    val messages: MessageRepository by lazy { MessageRepository(app) }
    val push: PushRegistrar by lazy { PushRegistrar(app) }
    val connection: ConnectionManager by lazy { ConnectionManager() }
    val settings: AppSettings by lazy { AppSettings(app) }
    val calls: CallManager by lazy { CallManager(app) }

    val userAgent: String get() = "agentring-android/${BuildConfig.VERSION_NAME} (Android ${Build.VERSION.RELEASE})"

    fun init(application: Application) {
        app = application
    }

    /** REST client for the current pairing, or null if not paired. */
    fun api(): RelayApi? = pairing.current?.let { RelayApi(it.relayUrl, it.deviceJwt, userAgent = userAgent) }

    fun deviceName(): String {
        val maker = Build.MANUFACTURER.replaceFirstChar { it.uppercase() }
        val model = Build.MODEL
        return (if (model.startsWith(maker, ignoreCase = true)) model else "$maker $model").take(64)
    }

    /** Redeems a scanned/pasted pairing code. Blocking; call from a background thread. */
    fun pairWith(rawCode: String): Pairing {
        val qr = QrPayload.parse(rawCode)
        val res = RelayApi(qr.relayUrl, null, userAgent = userAgent)
            .pair(PairRequest(pairingToken = qr.pairingToken, deviceName = deviceName(), appVersion = BuildConfig.VERSION_NAME))
        val p = Pairing(
            relayUrl = qr.relayUrl,
            relayId = res.relayId,
            deviceId = res.deviceId,
            deviceJwt = res.deviceJwt,
            pushSecret = res.pushSecret,
            agentName = res.agentName,
            pushConfig = res.pushConfig,
        )
        // In this order: whatever reads the pairing after the clear is the new one.
        pairing.save(p)
        messages.clear()
        calls.onActiveCall(null)
        connection.reconnect()
        appScope.launch(Dispatchers.IO) { push.ensureRegistered() }
        return p
    }

    /** Forgets the pairing locally (the relay keeps it until `agentring devices revoke`). */
    fun unpair() {
        connection.disconnect()
        Notifications.cancelMessages(app)
        pairing.clear()
        messages.clear()
        calls.onActiveCall(null)
        push.reset()
    }
}
