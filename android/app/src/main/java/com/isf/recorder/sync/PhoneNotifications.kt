package com.isf.recorder.sync

import android.content.Context
import androidx.core.app.NotificationCompat
import androidx.work.*
import com.isf.recorder.R
import com.isf.recorder.data.api.ISFApiClient
import com.isf.recorder.data.db.AppDatabase
import com.isf.recorder.data.prefs.UserPreferences
import com.isf.recorder.service.AudioRecordingService
import com.isf.recorder.service.RecorderNotifications
import java.util.concurrent.TimeUnit

/**
 * Phone notifications (21 Oct). No Firebase: the server saves each notification for the right person
 * (backend/notifications/inbox.py) and this collects the new ones every 15 min (Android's shortest
 * background interval) and on every app open, then shows them. A phone that never collected starts from
 * now, so old ones never arrive all at once. The phone also checks itself for recording pieces that have
 * not uploaded for 1 hour (at most one such notification every 6 h).
 */
object PhoneNotifications {
    private const val PERIODIC = "phone_notifications"
    private const val NOW = "phone_notifications_now"
    private const val BASE_ID = 8200                 // 8200..8299: server notifications
    private const val UPLOAD_STUCK_ID = 8050
    private const val STUCK_AFTER_MS = 60 * 60 * 1000L
    private const val STUCK_REPEAT_MS = 6 * 60 * 60 * 1000L

    private val networkConstraint get() = Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build()

    fun schedule(context: Context) {
        // No network constraint: the stuck-upload check matters most when there is no internet
        val req = PeriodicWorkRequestBuilder<PhoneNotificationsWorker>(15, TimeUnit.MINUTES).build()
        WorkManager.getInstance(context.applicationContext)
            .enqueueUniquePeriodicWork(PERIODIC, ExistingPeriodicWorkPolicy.KEEP, req)
    }

    /** On app open: collect right away. */
    fun collectNow(context: Context) {
        val req = OneTimeWorkRequestBuilder<PhoneNotificationsWorker>().setConstraints(networkConstraint).build()
        WorkManager.getInstance(context.applicationContext).enqueueUniqueWork(NOW, ExistingWorkPolicy.KEEP, req)
    }

    /** Which channel a server notification goes to. */
    fun channelFor(kind: String): String = when (kind) {
        "announcement" -> RecorderNotifications.CHANNEL_MESSAGES
        "admin_failed" -> RecorderNotifications.CHANNEL_ALERTS
        else -> RecorderNotifications.CHANNEL_REPORT
    }

    suspend fun collect(context: Context) {
        val prefs = UserPreferences(context)
        if (!prefs.isLoggedIn) return
        val cursor = prefs.notifyCursor
        val res = ISFApiClient(context).getNotificationInbox(cursor.takeIf { it >= 0 }).getOrNull() ?: return
        @Suppress("UNCHECKED_CAST")
        val items = (res["items"] as? List<Map<String, Any?>>).orEmpty()
        items.forEach { show(context, it) }
        (res["latest_id"] as? Number)?.toLong()?.let { if (it >= 0) prefs.notifyCursor = maxOf(it, cursor) }
    }

    private fun show(context: Context, n: Map<String, Any?>) {
        val id = (n["id"] as? Number)?.toLong() ?: return
        val kind = n["kind"]?.toString().orEmpty()
        val title = n["title"]?.toString().orEmpty().ifBlank { "Jomi" }
        val body = n["body"]?.toString().orEmpty()
        val recordingId = n["recording_id"]?.toString()?.takeIf { it.isNotBlank() && it != "null" }
        val notificationId = BASE_ID + (id % 100).toInt()
        RecorderNotifications.postRaw(context, notificationId,
            NotificationCompat.Builder(context, channelFor(kind))
                .setContentTitle(title)
                .setContentText(body.lineSequence().firstOrNull().orEmpty())
                .setStyle(NotificationCompat.BigTextStyle().bigText(body))
                .setSmallIcon(R.drawable.ic_stat_notify)
                .setContentIntent(RecorderNotifications.openAppPendingIntent(context, recordingId, notificationId))
                .setAutoCancel(true)
                .setPriority(if (kind == "announcement") NotificationCompat.PRIORITY_HIGH else NotificationCompat.PRIORITY_DEFAULT)
                .build())
    }

    /** Pure decision (unit-tested): pieces waiting longer than 1 h, not repeated within 6 h. */
    fun shouldWarnStuck(oldestPendingMs: Long?, now: Long, lastWarnedMs: Long, on: Boolean): Boolean =
        on && oldestPendingMs != null && now - oldestPendingMs >= STUCK_AFTER_MS && now - lastWarnedMs >= STUCK_REPEAT_MS

    suspend fun checkStuckUploads(context: Context, now: Long = System.currentTimeMillis()) {
        val prefs = UserPreferences(context)
        if (!prefs.isLoggedIn) return
        val current = AudioRecordingService.sessionState.value?.sessionId
        val pending = AppDatabase.getInstance(context).chunkDao().getPendingChunks().filter { it.sessionId != current }
        if (!shouldWarnStuck(pending.minOfOrNull { it.createdAt }, now, prefs.uploadStuckNotifiedMs, prefs.notifyUploadStuck)) return
        val n = pending.size
        val pieces = if (n == 1) "1 piece" else "$n pieces"
        RecorderNotifications.postRaw(context, UPLOAD_STUCK_ID,
            NotificationCompat.Builder(context, RecorderNotifications.CHANNEL_ALERTS)
                .setContentTitle("⏳ A recording has not uploaded yet")
                .setContentText("$pieces still on this phone. Connect to the internet and open Jomi.")
                .setStyle(NotificationCompat.BigTextStyle().bigText(
                    "$pieces of a recording are still on this phone after more than an hour. Connect to Wi-Fi or " +
                        "mobile data and open Jomi so it can upload. Do not uninstall the app or discard the session."))
                .setSmallIcon(R.drawable.ic_stat_notify)
                .setContentIntent(RecorderNotifications.openAppPendingIntent(context, null, UPLOAD_STUCK_ID))
                .setAutoCancel(true)
                .build())
        prefs.uploadStuckNotifiedMs = now
    }
}

class PhoneNotificationsWorker(context: Context, params: WorkerParameters) : CoroutineWorker(context, params) {
    override suspend fun doWork(): Result {
        runCatching { PhoneNotifications.checkStuckUploads(applicationContext) }
        runCatching { PhoneNotifications.collect(applicationContext) }
        return Result.success()
    }
}
