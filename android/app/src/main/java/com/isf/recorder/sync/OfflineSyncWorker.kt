package com.isf.recorder.sync

import android.content.Context
import android.util.Log
import androidx.work.CoroutineWorker
import androidx.work.WorkerParameters
import com.isf.recorder.data.db.AppDatabase
import com.isf.recorder.data.model.SessionState
import com.isf.recorder.service.AudioRecordingService

/**
 * Uploads pending chunks of every session. Never calls /finish itself (bug I): for stopped sessions
 * whose chunks are all on the server it queues the session's unique FinishSessionWorker.
 * Loops until no pending chunk is left so chunks queued during the run are not missed (KEEP policy).
 */
class OfflineSyncWorker(
    appContext: Context,
    workerParams: WorkerParameters
) : CoroutineWorker(appContext, workerParams) {

    override suspend fun doWork(): Result {
        val db = AppDatabase.getInstance(applicationContext)
        val chunkDao = db.chunkDao()
        val sessionDao = db.sessionDao()
        val uploader = ChunkUploader(applicationContext)
        val affected = mutableSetOf<String>()
        var failed = false

        var passes = 0
        while (passes++ < 20) {
            val pending = chunkDao.getPendingChunks()
            if (pending.isEmpty()) break
            Log.i(TAG, "Pass $passes: ${pending.size} pending chunks")
            var progress = false
            for (chunk in pending) {
                if (isStopped) return Result.retry()
                when (uploader.upload(chunk)) {
                    ChunkUploader.UploadResult.UPLOADED,
                    ChunkUploader.UploadResult.ALREADY_UPLOADED,
                    ChunkUploader.UploadResult.FILE_MISSING -> { progress = true; affected += chunk.sessionId }
                    ChunkUploader.UploadResult.OFFLINE,
                    ChunkUploader.UploadResult.FAILED -> { failed = true; break }
                }
            }
            if (failed || !progress) break
        }

        for (sessionId in affected) {
            if (AudioRecordingService.isLiveSession(sessionId)) continue
            if (chunkDao.getPendingChunksForSession(sessionId).isNotEmpty()) continue
            val row = sessionDao.get(sessionId)
            // row == null: a v1.2.0 session from before the update — finish it as the old app did.
            if (row == null || row.state == SessionState.STOPPED) {
                SessionFinisher.enqueueFinishWorker(applicationContext, sessionId)
            }
        }
        return if (failed) Result.retry() else Result.success()
    }

    private companion object {
        const val TAG = "OfflineSyncWorker"
    }
}
