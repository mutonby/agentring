package dev.agentring.app.notify

import android.Manifest
import android.annotation.SuppressLint
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.app.Person
import androidx.core.app.RemoteInput
import androidx.core.content.ContextCompat
import dev.agentring.app.Graph
import dev.agentring.app.R
import dev.agentring.app.ui.MainActivity
import dev.agentring.core.Message

object Notifications {
    const val CHANNEL_MESSAGES = "messages"
    const val ID_MESSAGES = 1001
    const val ID_TEST = 1002
    const val ID_REPLY_FAILED = 1003
    const val KEY_REPLY = "reply_text"

    fun createChannels(context: Context) {
        val nm = context.getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(
            NotificationChannel(CHANNEL_MESSAGES, context.getString(R.string.channel_messages), NotificationManager.IMPORTANCE_HIGH),
        )
    }

    fun canNotify(context: Context): Boolean =
        Build.VERSION.SDK_INT < 33 ||
            ContextCompat.checkSelfPermission(context, Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED

    private fun mutableFlag() = if (Build.VERSION.SDK_INT >= 31) PendingIntent.FLAG_MUTABLE else 0

    fun openMessagesIntent(context: Context): PendingIntent {
        val i = Intent(context, MainActivity::class.java)
            .putExtra(MainActivity.EXTRA_OPEN, MainActivity.OPEN_MESSAGES)
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP)
        return PendingIntent.getActivity(context, 0, i, PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
    }

    private fun replyAction(context: Context): NotificationCompat.Action {
        val intent = Intent(context, ReplyReceiver::class.java)
        val pi = PendingIntent.getBroadcast(context, 0, intent, PendingIntent.FLAG_UPDATE_CURRENT or mutableFlag())
        val input = RemoteInput.Builder(KEY_REPLY).setLabel(context.getString(R.string.reply)).build()
        return NotificationCompat.Action.Builder(R.drawable.ic_send, context.getString(R.string.reply), pi)
            .addRemoteInput(input)
            .setAllowGeneratedReplies(true)
            .setSemanticAction(NotificationCompat.Action.SEMANTIC_ACTION_REPLY)
            .setShowsUserInterface(false)
            .build()
    }

    private fun agentPerson(name: String) = Person.Builder().setName(name).setKey("agent").build()

    /**
     * Shows (or updates) the conversation notification with the agent's unread
     * messages, plus an optional reply the user just sent from the notification.
     */
    @SuppressLint("MissingPermission")
    fun showMessages(
        context: Context,
        agentName: String,
        agentMessages: List<Message> = Graph.messages.unreadFromAgent(),
        sentReply: String? = null,
    ) {
        if (!canNotify(context)) return
        val unread = agentMessages.takeLast(6)
        if (unread.isEmpty() && sentReply == null) return
        val me = Person.Builder().setName(context.getString(R.string.you)).build()
        val agent = agentPerson(agentName)
        val style = NotificationCompat.MessagingStyle(me)
        unread.forEach { style.addMessage(it.text, it.createdAt, agent) }
        if (sentReply != null) style.addMessage(sentReply, System.currentTimeMillis(), null as Person?)

        val n = NotificationCompat.Builder(context, CHANNEL_MESSAGES)
            .setSmallIcon(R.drawable.ic_notification)
            .setStyle(style)
            .setCategory(NotificationCompat.CATEGORY_MESSAGE)
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .setContentIntent(openMessagesIntent(context))
            .setAutoCancel(true)
            .setOnlyAlertOnce(sentReply != null)
            .addAction(replyAction(context))
            .build()
        NotificationManagerCompat.from(context).notify(ID_MESSAGES, n)
    }

    @SuppressLint("MissingPermission")
    fun showReplyFailed(context: Context, text: String, error: String) {
        if (!canNotify(context)) return
        val n = NotificationCompat.Builder(context, CHANNEL_MESSAGES)
            .setSmallIcon(R.drawable.ic_notification)
            .setContentTitle(context.getString(R.string.reply_failed_title))
            .setContentText("$text — $error")
            .setStyle(NotificationCompat.BigTextStyle().bigText("$text\n\n$error"))
            .setContentIntent(openMessagesIntent(context))
            .setAutoCancel(true)
            .build()
        NotificationManagerCompat.from(context).notify(ID_REPLY_FAILED, n)
    }

    @SuppressLint("MissingPermission")
    fun showTest(context: Context) {
        if (!canNotify(context)) return
        val name = Graph.pairing.current?.agentName ?: context.getString(R.string.app_name)
        val style = NotificationCompat.MessagingStyle(Person.Builder().setName(context.getString(R.string.you)).build())
            .addMessage(context.getString(R.string.test_notification_text), System.currentTimeMillis(), agentPerson(name))
        val n = NotificationCompat.Builder(context, CHANNEL_MESSAGES)
            .setSmallIcon(R.drawable.ic_notification)
            .setStyle(style)
            .setCategory(NotificationCompat.CATEGORY_MESSAGE)
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .setAutoCancel(true)
            .build()
        NotificationManagerCompat.from(context).notify(ID_TEST, n)
    }

    fun cancelMessages(context: Context) {
        NotificationManagerCompat.from(context).cancel(ID_MESSAGES)
    }
}
