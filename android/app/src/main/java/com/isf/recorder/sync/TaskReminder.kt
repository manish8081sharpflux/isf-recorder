package com.isf.recorder.sync

import android.content.Context
import androidx.core.app.NotificationCompat
import androidx.work.*
import com.isf.recorder.R
import com.isf.recorder.data.api.ISFApiClient
import com.isf.recorder.data.prefs.UserPreferences
import com.isf.recorder.service.RecorderNotifications
import java.time.Duration
import java.time.LocalDateTime
import java.time.LocalTime
import java.util.concurrent.TimeUnit

/**
 * Daily task reminder (14 Oct): one phone notification at the person's chosen time, "You have N open tasks",
 * only when the reminder is on and something is open. The setting lives on the server (/task-reminder).
 */
object TaskReminder {
    private const val WORK = "task_reminder_daily"
    private const val NOTIFICATION_ID = 8030

    /** (Re)plan the daily run at HH:MM ("" = keep the current plan); off cancels it. */
    fun schedule(context: Context, on: Boolean, time: String) {
        val wm = WorkManager.getInstance(context.applicationContext)
        if (!on) {
            wm.cancelUniqueWork(WORK)
            return
        }
        val at = runCatching { LocalTime.parse(time) }.getOrDefault(LocalTime.of(9, 0))
        val now = LocalDateTime.now()
        var next = now.toLocalDate().atTime(at)
        if (!next.isAfter(now)) next = next.plusDays(1)
        val req = PeriodicWorkRequestBuilder<TaskReminderWorker>(24, TimeUnit.HOURS)
            .setInitialDelay(Duration.between(now, next).toMinutes(), TimeUnit.MINUTES)
            .setConstraints(Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build())
            .build()
        wm.enqueueUniquePeriodicWork(WORK, ExistingPeriodicWorkPolicy.UPDATE, req)
    }

    fun notifyOpenTasks(context: Context, open: Int) {
        val text = if (open == 1) "You have 1 open task" else "You have $open open tasks"
        RecorderNotifications.postRaw(context, NOTIFICATION_ID,
            NotificationCompat.Builder(context, RecorderNotifications.CHANNEL_REPORT)
                .setContentTitle("✅ $text")
                .setContentText("Tap to see them in Calls → My tasks")
                .setSmallIcon(R.drawable.ic_stat_notify)
                .setContentIntent(RecorderNotifications.openAppPendingIntent(context, null, NOTIFICATION_ID))
                .setAutoCancel(true)
                .build())
    }
}

class TaskReminderWorker(context: Context, params: WorkerParameters) : CoroutineWorker(context, params) {
    override suspend fun doWork(): Result {
        if (UserPreferences(applicationContext).token.isNullOrBlank()) return Result.success()
        val r = ISFApiClient(applicationContext).getTaskReminder().getOrNull() ?: return Result.retry()
        val on = r["on"] as? Boolean ?: true
        val open = (r["open_count"] as? Number)?.toInt() ?: 0
        if (on && open > 0) TaskReminder.notifyOpenTasks(applicationContext, open)
        return Result.success()
    }
}
