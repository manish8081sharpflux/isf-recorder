package com.isf.recorder.sync

import android.content.Context
import android.util.Log
import androidx.work.CoroutineWorker
import androidx.work.WorkerParameters
import com.isf.recorder.data.db.AppDatabase
import com.isf.recorder.data.model.SessionState
import com.isf.recorder.service.AudioRecordingService
import com.isf.recorder.service.OrphanRecovery

/**
 * Finishes one stopped session that Stop could not finish inline (offline, killed app, orphan).
 * Unique per session (KEEP). Uploads the remaining chunks, then calls /finish with source "sync_worker".
 * Retries (exponential backoff) until it succeeds — audio is never given up.
 */
class FinishSessionWorker(appContext: Context, params: WorkerParameters) : CoroutineWorker(appContext, params) {

    companion object {
        const val KEY_SESSION_ID = "session_id"
        private const val TAG = "FinishSessionWorker"
        /** Chunk files younger than this may still be being finalized by this process. */
        private const val MIN_ORPHAN_FILE_AGE_MS = 30_000L
    }

    override suspend fun doWork(): Result {
        val sessionId = inputData.getString(KEY_SESSION_ID) ?: return Result.failure()
        if (AudioRecordingService.isLiveSession(sessionId)) {
            Log.i(TAG, "$sessionId is still owned by the recording service; it will finish it")
            return Result.success()
        }
        val row = AppDatabase.getInstance(applicationContext).sessionDao().get(sessionId)
        if (row?.state == SessionState.ACTIVE) return Result.retry()   // orphan recovery marks it STOPPED first

        // Pick up a chunk file that was closed but never registered in Room.
        val repair = OrphanRecovery.registerOrphanFiles(
            applicationContext, sessionId,
            notModifiedAfterMs = System.currentTimeMillis() - MIN_ORPHAN_FILE_AGE_MS
        )
        if (repair.youngSkipped > 0) return Result.retry()

        val result = SessionFinisher.uploadAndFinish(applicationContext, sessionId, source = "sync_worker")
        return when (result.outcome) {
            SessionFinisher.Outcome.FINISHED -> {
                AudioRecordingService.onFinishedByWorker(sessionId, result.recordingId)
                if (result.recordingId != null && result.totalChunks > 0) {
                    ReportStatusWorker.enqueue(applicationContext, result.recordingId, row?.title)
                }
                Result.success()
            }
            SessionFinisher.Outcome.ALREADY_FINISHED, SessionFinisher.Outcome.DISCARDED -> Result.success()
            SessionFinisher.Outcome.PENDING -> Result.retry()
        }
    }
}
