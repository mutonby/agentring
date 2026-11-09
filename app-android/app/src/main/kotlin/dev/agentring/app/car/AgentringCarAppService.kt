package dev.agentring.app.car

import android.annotation.SuppressLint
import android.content.Intent
import android.content.pm.ApplicationInfo
import androidx.car.app.CarAppService
import androidx.car.app.Screen
import androidx.car.app.Session
import androidx.car.app.validation.HostValidator

/**
 * Android Auto entry point (calling category): a "Call <agent>" screen in the car. Calls
 * themselves show in Android Auto's own in-call view, because they are Telecom calls
 * ([dev.agentring.app.call.TelecomBridge]); answer, hang up and mute work from the car.
 *
 * Declared in the `:auto` module's manifest, which release builds include only with
 * `-PagentringAndroidAuto=true` (docs/android-auto.md).
 */
class AgentringCarAppService : CarAppService() {
    // The library's list of Android Auto / Automotive hosts, as Google's samples use it.
    @SuppressLint("PrivateResource")
    override fun createHostValidator(): HostValidator =
        if (applicationInfo.flags and ApplicationInfo.FLAG_DEBUGGABLE != 0) {
            HostValidator.ALLOW_ALL_HOSTS_VALIDATOR // the Desktop Head Unit, during development
        } else {
            HostValidator.Builder(applicationContext)
                .addAllowedHosts(androidx.car.app.R.array.hosts_allowlist_sample)
                .build()
        }

    override fun onCreateSession(): Session = AgentSession()
}

private class AgentSession : Session() {
    override fun onCreateScreen(intent: Intent): Screen = CallAgentScreen(carContext)
}
