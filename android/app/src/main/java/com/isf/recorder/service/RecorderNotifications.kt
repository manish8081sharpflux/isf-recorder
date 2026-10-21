package com.isf.recorder.service

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.os.Build
import android.util.Log
import androidx.core.app.NotificationCompat
import com.isf.recorder.MainActivity
import com.isf.recorder.R
import com.isf.recorder.data.model.RecordingStatus
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.TimeZone

/** All recorder notification channels and notifications (single place; channels created once in ISFApplication). */
object RecorderNotifications {
    private const val TAG = "RecorderNotifications"

    const val CHANNEL_RECORDING = "isf_recording_channel"
    const val CHANNEL_ALERTS = "isf_recording_alerts"
    const val CHANNEL_REPORT = "isf_report_channel"
    const val CHANNEL_UPDATES = "isf_app_updates"

    /** Extra on the MainActivity intent: open this recording's Report screen (UI handles it). */
    const val EXTRA_OPEN_RECORDING_ID = "open_recording_id"

    const val FOREGROUND_ID = 8019
    const val ALERT_ID = 8020
    const val REMINDER_ID = 8021
    private const val INTERRUPTED_BASE = 8100
    private const val REPORT_BASE = 9000

    fun ensureChannels(context: Context) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
        val nm = context.getSystemService(NotificationManager::class.java) ?: return
        nm.createNotificationChannel(
            NotificationChannel(CHANNEL_RECORDING, "Recording status", NotificationManager.IMPORTANCE_LOW).apply {
                description = "Shows the ongoing recording with Pause/Resume and Stop"
                setShowBadge(false)
            }
        )
        nm.createNotificationChannel(
            NotificationChannel(CHANNEL_ALERTS, "Recording alerts", NotificationManager.IMPORTANCE_HIGH).apply {
                description = "Calls pausing the recording, microphone changes, reminders, interrupted recordings"
                enableVibration(true)
                setShowBadge(true)
            }
        )
        nm.createNotificationChannel(
            NotificationChannel(CHANNEL_REPORT, "Reports", NotificationManager.IMPORTANCE_DEFAULT).apply {
                description = "Report ready / transcription failed"
                setShowBadge(true)
            }
        )
        nm.createNotificationChannel(
            NotificationChannel(CHANNEL_UPDATES, "App updates", NotificationManager.IMPORTANCE_HIGH).apply {
                description = "A new version of Jomi is available"
                setShowBadge(true)
            }
        )
    }

    // ── Pure text helpers (unit-tested) ─────────────────────────────────────────

    data class ForegroundContent(
        val title: String,
        val text: String,
        val showPause: Boolean,
        val showResume: Boolean,
        val showStop: Boolean
    )

    fun formatDuration(totalSec: Int): String {
        val s = totalSec.coerceAtLeast(0)
        val h = s / 3600
        val m = (s % 3600) / 60
        val sec = s % 60
        return if (h > 0) String.format(Locale.US, "%d:%02d:%02d", h, m, sec) else String.format(Locale.US, "%02d:%02d", m, sec)
    }

    fun formatClock(ms: Long, zone: TimeZone = TimeZone.getDefault()): String =
        SimpleDateFormat("HH:mm", Locale.US).apply { timeZone = zone }.format(Date(ms))

    fun interruptedText(startedAtMs: Long, zone: TimeZone = TimeZone.getDefault()): String =
        "Your recording from ${formatClock(startedAtMs, zone)} was interrupted and has been saved"

    fun reminderText(hours: Int): String = "Still recording · $hours h"

    fun foregroundContent(
        status: RecordingStatus,
        voiceActive: Boolean,
        voiceSeconds: Int,
        inCall: Boolean,
        micMessage: String?
    ): ForegroundContent {
        val saved = "${formatDuration(voiceSeconds)} saved"
        return when (status) {
            RecordingStatus.RECORDING ->
                if (voiceActive) ForegroundContent("Recording", saved, showPause = true, showResume = false, showStop = true)
                else ForegroundContent("Listening — silence not saved", saved, showPause = true, showResume = false, showStop = true)
            RecordingStatus.PAUSED_USER ->
                ForegroundContent("Paused", saved, showPause = false, showResume = true, showStop = true)
            RecordingStatus.PAUSED_CONFLICT ->
                if (inCall) ForegroundContent("Paused — microphone in use by a call", "Resumes automatically when the call ends · $saved",
                    showPause = false, showResume = true, showStop = true)
                else ForegroundContent("Paused — microphone unavailable", micMessage ?: "Retrying automatically · $saved",
                    showPause = false, showResume = true, showStop = true)
            RecordingStatus.FINALIZING ->
                ForegroundContent("Saving recording…", "Uploading audio · $saved", showPause = false, showResume = false, showStop = false)
            RecordingStatus.IDLE ->
                ForegroundContent("ISF Recorder", "", showPause = false, showResume = false, showStop = false)
        }
    }

    // ── Builders ────────────────────────────────────────────────────────────────

    fun openAppPendingIntent(context: Context, recordingId: String? = null, requestCode: Int = 0): PendingIntent {
        val intent = Intent(context, MainActivity::class.java).apply {
            flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP
            if (recordingId != null) putExtra(EXTRA_OPEN_RECORDING_ID, recordingId)
        }
        return PendingIntent.getActivity(context, requestCode, intent,
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
    }

    private fun serviceAction(context: Context, action: String, requestCode: Int): PendingIntent =
        PendingIntent.getService(
            context, requestCode,
            Intent(context, AudioRecordingService::class.java).setAction(action),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        )

    fun buildForeground(context: Context, content: ForegroundContent): Notification {
        val b = NotificationCompat.Builder(context, CHANNEL_RECORDING)
            .setContentTitle(content.title)
            .setContentText(content.text)
            .setSmallIcon(R.drawable.ic_stat_notify)
            .setContentIntent(openAppPendingIntent(context))
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setSilent(true)
            .setCategory(NotificationCompat.CATEGORY_SERVICE)
            .setPriority(NotificationCompat.PRIORITY_LOW)
        if (content.showPause) b.addAction(android.R.drawable.ic_media_pause, "Pause",
            serviceAction(context, AudioRecordingService.ACTION_PAUSE, 1))
        if (content.showResume) b.addAction(android.R.drawable.ic_media_play, "Resume",
            serviceAction(context, AudioRecordingService.ACTION_RESUME, 2))
        if (content.showStop) b.addAction(android.R.drawable.ic_menu_close_clear_cancel, "Stop",
            serviceAction(context, AudioRecordingService.ACTION_STOP, 3))
        return b.build()
    }

    private fun post(context: Context, id: Int, notification: Notification) {
        try {
            context.getSystemService(NotificationManager::class.java)?.notify(id, notification)
        } catch (e: Exception) {
            Log.w(TAG, "notify($id) failed: ${e.message}")
        }
    }

    fun alert(context: Context, title: String, text: String, id: Int = ALERT_ID) {
        post(context, id, NotificationCompat.Builder(context, CHANNEL_ALERTS)
            .setContentTitle(title)
            .setContentText(text)
            .setStyle(NotificationCompat.BigTextStyle().bigText(text))
            .setSmallIcon(R.drawable.ic_stat_notify)
            .setContentIntent(openAppPendingIntent(context))
            .setAutoCancel(true)
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .setDefaults(NotificationCompat.DEFAULT_ALL)
            .build())
    }

    fun notifyReminder(context: Context, hours: Int) =
        alert(context, reminderText(hours), "Recording continues. Tap to open, or Stop when you are done.", REMINDER_ID)

    fun notifyInterrupted(context: Context, sessionId: String, startedAtMs: Long) =
        alert(context, "Recording ended — the phone closed the app", interruptedText(startedAtMs),
            INTERRUPTED_BASE + (sessionId.hashCode() and 0x3ff))

    /** Post an already-built notification (e.g. the daily task reminder). */
    fun postRaw(context: Context, id: Int, notification: Notification) = post(context, id, notification)

    fun notifyReport(context: Context, recordingId: String, title: String, text: String) {
        val requestCode = REPORT_BASE + (recordingId.hashCode() and 0x3ff)
        post(context, requestCode, NotificationCompat.Builder(context, CHANNEL_REPORT)
            .setContentTitle(title)
            .setContentText(text)
            .setSmallIcon(R.drawable.ic_stat_notify)
            .setContentIntent(openAppPendingIntent(context, recordingId, requestCode))
            .setAutoCancel(true)
            .setPriority(NotificationCompat.PRIORITY_DEFAULT)
            .build())
    }
}
