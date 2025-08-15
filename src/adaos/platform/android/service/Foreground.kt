package ai.adaos.platform.android

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.content.Context
import android.os.Build
import androidx.core.app.NotificationCompat

object Foreground {
    fun ensureChannel(ctx: Context, channelId: String, name: String) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val nm = ctx.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
            if (nm.getNotificationChannel(channelId) == null) {
                val ch = NotificationChannel(channelId, name, NotificationManager.IMPORTANCE_LOW)
                nm.createNotificationChannel(ch)
            }
        }
    }

    fun buildNotification(
        ctx: Context,
        channelId: String,
        title: String,
        text: String,
        notifId: Int
    ): Notification {
        val builder = NotificationCompat.Builder(ctx, channelId)
            .setContentTitle(title)
            .setContentText(text)
            .setSmallIcon(android.R.drawable.ic_btn_speak_now)
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setPriority(NotificationCompat.PRIORITY_LOW)

        return builder.build()
    }
}
