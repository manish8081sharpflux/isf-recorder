package com.isf.recorder.sync

import android.content.Context
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.work.*
import com.isf.recorder.BuildConfig
import com.isf.recorder.R
import com.isf.recorder.data.api.ISFApiClient
import com.isf.recorder.data.prefs.UserPreferences
import com.isf.recorder.service.RecorderNotifications
import com.isf.recorder.ui.components.AppUpdate
import com.isf.recorder.ui.components.UpdateCheck
import java.util.concurrent.TimeUnit

/**
 * "New version available" phone notification (21 Oct). No Firebase: the phone itself asks the server
 * (/app/latest, same answer as the in-app popup) every few hours in the background and posts the
 * notification ONCE per new version, for every logged-in user whose app is older. Tapping it opens Jomi,
 * which then shows the download popup. TEST builds only when the server says so (notify_test_builds).
 */
object UpdateNotifier {
    private const val WORK = "app_update_check"
    const val NOTIFICATION_ID = 8040

    fun schedule(context: Context) {
        val req = PeriodicWorkRequestBuilder<UpdateNotifyWorker>(3, TimeUnit.HOURS)
            .setConstraints(Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build())
            .build()
        WorkManager.getInstance(context.applicationContext)
            .enqueueUniquePeriodicWork(WORK, ExistingPeriodicWorkPolicy.KEEP, req)
    }

    /** Posts the notification unless it was already shown for this version. */
    fun notifyIfNew(context: Context, update: AppUpdate?) {
        val prefs = UserPreferences(context)
        if (!UpdateCheck.shouldNotify(update, prefs.lastNotifiedUpdateCode)) return
        val u = update ?: return
        val name = "Jomi ${u.versionName}".trim()
        RecorderNotifications.postRaw(context, NOTIFICATION_ID,
            NotificationCompat.Builder(context, RecorderNotifications.CHANNEL_UPDATES)
                .setContentTitle("⬆️ $name is available")
                .setContentText(u.notes.lineSequence().firstOrNull()?.takeIf { it.isNotBlank() } ?: "Tap to update the app")
                .setStyle(NotificationCompat.BigTextStyle().bigText(
                    (u.notes.trim().takeIf { it.isNotBlank() }?.let { "$it\n\n" } ?: "") + "Tap to update the app"))
                .setSmallIcon(R.drawable.ic_stat_notify)
                .setContentIntent(RecorderNotifications.openAppPendingIntent(context, null, NOTIFICATION_ID))
                .setAutoCancel(true)
                .setPriority(NotificationCompat.PRIORITY_HIGH)
                .build())
        prefs.lastNotifiedUpdateCode = u.versionCode
    }

    /** The app is up to date: remove an old "update available" notification. */
    fun clear(context: Context) {
        NotificationManagerCompat.from(context).cancel(NOTIFICATION_ID)
    }
}

class UpdateNotifyWorker(context: Context, params: WorkerParameters) : CoroutineWorker(context, params) {
    override suspend fun doWork(): Result {
        val prefs = UserPreferences(applicationContext)
        if (!prefs.isLoggedIn) return Result.success()
        val latest = ISFApiClient(applicationContext).getLatestApp().getOrNull() ?: return Result.retry()
        val update = UpdateCheck.decide(latest, BuildConfig.VERSION_CODE, BuildConfig.APPLICATION_ID)
        if (update == null) UpdateNotifier.clear(applicationContext) else UpdateNotifier.notifyIfNew(applicationContext, update)
        return Result.success()
    }
}
