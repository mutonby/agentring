package dev.agentring.app.call

import android.app.Notification
import android.app.NotificationManager
import android.content.Context
import android.provider.Settings

/**
 * The user's "sensitive content on the lock screen" choice. The system applies it to our
 * notifications by itself; the call screen shows over the lock screen, so it has to ask.
 */
object LockScreenPrivacy {
    // Not in the public API, but readable by apps. If a device refuses, the reason stays visible.
    private const val ALLOW_PRIVATE = "lock_screen_allow_private_notifications"
    private const val SHOW_NOTIFICATIONS = "lock_screen_show_notifications"

    /** The user hides sensitive content on the lock screen, for the whole phone or for our calls. */
    fun hidesSensitiveContent(context: Context): Boolean {
        val resolver = context.contentResolver
        val phoneWide = runCatching {
            Settings.Secure.getInt(resolver, ALLOW_PRIVATE, 1) == 0 || Settings.Secure.getInt(resolver, SHOW_NOTIFICATIONS, 1) == 0
        }.getOrDefault(false)
        val nm = context.getSystemService(NotificationManager::class.java)
        return phoneWide || listOf(CallNotifications.CHANNEL_CALLS, CallNotifications.CHANNEL_CALLS_URGENT).any { id ->
            nm.getNotificationChannel(id)?.lockscreenVisibility.let {
                it == Notification.VISIBILITY_PRIVATE || it == Notification.VISIBILITY_SECRET
            }
        }
    }
}
