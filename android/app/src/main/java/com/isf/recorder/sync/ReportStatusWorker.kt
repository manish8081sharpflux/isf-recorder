package com.isf.recorder.sync

import android.content.Context
import android.util.Log
import androidx.work.*
import com.isf.recorder.data.api.ISFApiClient
import com.isf.recorder.service.RecorderNotifications
import java.util.concurrent.TimeUnit

/**
 * After Stop: polls GET /recordings/status for one recording (linear backoff 30 s, 60 s, 90 s …,
 * network constraint) and posts "Report ready: <title>" or "Transcription failed — tap to retry" on
 * channel isf_report_channel. Tapping opens MainActivity with extra "open_recording_id".
 * Gives up after 2 h. Unique per recording.
 */
class ReportStatusWorker(appContext: Context, params: WorkerParameters) : CoroutineWorker(appContext, params) {

    sealed class Decision {
        object KeepPolling : Decision()
        object GiveUp : Decision()
        data class Notify(val title: String, val text: String) : Decision()
    }

    companion object {
        private const val TAG = "ReportStatusWorker"
        const val KEY_RECORDING_ID = "recording_id"
        const val KEY_TITLE = "title"
        const val KEY_STARTED_AT = "started_at"
        const val GIVE_UP_AFTER_MS = 2 * 60 * 60 * 1000L
        /** status=completed but no report row after this long → just say the transcript is ready. */
        const val NO_REPORT_GRACE_MS = 20 * 60 * 1000L

        fun workName(recordingId: String) = "report_status_$recordingId"

        /** [replace] = true restarts the 2-h window (e.g. after Retry). */
        fun enqueue(context: Context, recordingId: String, title: String?, replace: Boolean = false) {
            val request = OneTimeWorkRequestBuilder<ReportStatusWorker>()
                .setInputData(workDataOf(
                    KEY_RECORDING_ID to recordingId,
                    KEY_TITLE to title,
                    KEY_STARTED_AT to System.currentTimeMillis()
                ))
                .setConstraints(Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build())
                .setInitialDelay(30, TimeUnit.SECONDS)
                .setBackoffCriteria(BackoffPolicy.LINEAR, 30, TimeUnit.SECONDS)
                .build()
            WorkManager.getInstance(context.applicationContext).enqueueUniqueWork(
                workName(recordingId),
                if (replace) ExistingWorkPolicy.REPLACE else ExistingWorkPolicy.KEEP,
                request
            )
        }

        /** Pure decision from one /recordings/status item (null = not returned). */
        fun decide(item: Map<String, Any?>?, fallbackTitle: String?, elapsedMs: Long): Decision {
            val title = (item?.get("title") as? String)?.takeIf { it.isNotBlank() } ?: fallbackTitle ?: "your recording"
            val status = item?.get("status") as? String
            val report = item?.get("report_status") as? String
            return when {
                status == "error" -> Decision.Notify("Transcription failed — tap to retry", title)
                report == "draft" || report == "final" -> Decision.Notify("Report ready: $title", "Tap to open the report")
                report == "error" -> Decision.Notify("Report could not be generated", "$title — tap to open")
                status == "completed" && title == "No speech detected" ->
                    Decision.Notify("No speech detected", "Nothing to transcribe in this recording")
                status == "completed" && report == null && elapsedMs >= NO_REPORT_GRACE_MS ->
                    Decision.Notify("Transcript ready: $title", "Tap to open")
                elapsedMs >= GIVE_UP_AFTER_MS -> Decision.GiveUp
                else -> Decision.KeepPolling
            }
        }
    }

    override suspend fun doWork(): Result {
        val recordingId = inputData.getString(KEY_RECORDING_ID) ?: return Result.failure()
        val fallbackTitle = inputData.getString(KEY_TITLE)
        val startedAt = inputData.getLong(KEY_STARTED_AT, System.currentTimeMillis())
        val elapsed = System.currentTimeMillis() - startedAt

        val res = ISFApiClient(applicationContext).getRecordingStatuses(listOf(recordingId))
        if (res.isFailure) {
            Log.w(TAG, "Status poll failed for $recordingId: ${res.exceptionOrNull()?.message}")
            return if (elapsed >= GIVE_UP_AFTER_MS) Result.success() else Result.retry()
        }
        val item = res.getOrNull()?.firstOrNull { it["id"]?.toString() == recordingId }
        return when (val d = decide(item, fallbackTitle, elapsed)) {
            is Decision.Notify -> {
                RecorderNotifications.notifyReport(applicationContext, recordingId, d.title, d.text)
                Result.success()
            }
            Decision.GiveUp -> {
                Log.i(TAG, "Giving up on $recordingId after ${elapsed / 60000} min")
                Result.success()
            }
            Decision.KeepPolling -> Result.retry()
        }
    }
}
