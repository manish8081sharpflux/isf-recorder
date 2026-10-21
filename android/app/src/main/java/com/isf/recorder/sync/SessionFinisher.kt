package com.isf.recorder.sync

import android.content.Context
import android.util.Log
import androidx.work.*
import com.google.gson.Gson
import com.isf.recorder.data.api.ISFApiClient
import com.isf.recorder.data.db.AppDatabase
import com.isf.recorder.data.model.SessionState
import com.isf.recorder.service.AudioChunkManager
import kotlinx.coroutines.delay
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import java.io.IOException
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.TimeUnit
import kotlin.math.roundToInt

/**
 * The single place that calls /finish (bug I): upload every pending chunk of the session, then
 * finish exactly once. Guarded per session by a process-wide mutex and by the persisted
 * FINISHED state. Used by Stop (source "client") and FinishSessionWorker (source "sync_worker").
 */
object SessionFinisher {
    private const val TAG = "SessionFinisher"
    private val locks = ConcurrentHashMap<String, Mutex>()
    private val gson = Gson()

    enum class Outcome { FINISHED, ALREADY_FINISHED, DISCARDED, PENDING }

    data class Result(val outcome: Outcome, val recordingId: String? = null, val totalChunks: Int = 0)

    /**
     * @param deadlineMs give up uploading (→ PENDING) after this wall-clock time; the caller then
     *                   relies on [enqueueFinishWorker].
     */
    suspend fun uploadAndFinish(
        context: Context,
        sessionId: String,
        source: String,
        deadlineMs: Long = Long.MAX_VALUE
    ): Result = locks.getOrPut(sessionId) { Mutex() }.withLock {
        val app = context.applicationContext
        val db = AppDatabase.getInstance(app)
        val sessionDao = db.sessionDao()
        val chunkDao = db.chunkDao()
        val uploader = ChunkUploader(app)

        val row = sessionDao.get(sessionId)
        when (row?.state) {
            SessionState.FINISHED -> return@withLock Result(Outcome.ALREADY_FINISHED, row.recordingId, row.totalChunks)
            SessionState.DISCARDED -> return@withLock Result(Outcome.DISCARDED)
            SessionState.ACTIVE -> return@withLock Result(Outcome.PENDING)   // must be stopped first
        }

        // 1. Upload everything still pending for this session.
        var failedPasses = 0
        while (true) {
            val pending = chunkDao.getPendingChunksForSession(sessionId)
            if (pending.isEmpty()) break
            if (System.currentTimeMillis() > deadlineMs) return@withLock Result(Outcome.PENDING)
            var passFailed = false
            for (chunk in pending) {
                val r = uploader.upload(chunk)
                if (r == ChunkUploader.UploadResult.OFFLINE) return@withLock Result(Outcome.PENDING)
                if (r == ChunkUploader.UploadResult.FAILED) { passFailed = true; break }
            }
            if (passFailed) {
                failedPasses++
                if (failedPasses >= 3) return@withLock Result(Outcome.PENDING)
                delay(2_000L * failedPasses)
            }
        }
        if (!SessionStarter.ensureStarted(app, sessionId)) return@withLock Result(Outcome.PENDING)

        // 2. Finish once.
        val maxIndex = chunkDao.getMaxChunkIndex(sessionId) ?: -1
        val totalChunks = maxOf(row?.totalChunks ?: 0, maxIndex + 1)
        val chunkSeconds = chunkDao.getAllChunksForSession(sessionId).sumOf { it.durationSeconds.toDouble() }.roundToInt()
        val voiceSeconds = maxOf(row?.voiceSeconds ?: 0, chunkSeconds)
        val res = ISFApiClient(app).finishRecording(
            sessionId = sessionId,
            totalChunks = totalChunks,
            durationSeconds = voiceSeconds,          // saved audio length (silence is not saved)
            voiceSeconds = voiceSeconds,
            silenceSkippedSeconds = row?.silenceSkippedSeconds ?: 0,
            source = source,
            silenceGapsJson = row?.silenceGapsJson
        )
        if (res.isFailure) {
            val e = res.exceptionOrNull()
            Log.w(TAG, "/finish for $sessionId failed (${if (e is IOException) "network" else "server"}): ${e?.message}")
            return@withLock Result(Outcome.PENDING)
        }
        val body = try {
            @Suppress("UNCHECKED_CAST")
            gson.fromJson(res.getOrNull() ?: "{}", Map::class.java) as? Map<String, Any?>
        } catch (e: Exception) { null }
        if (body?.get("status") == "discarded") {
            Log.i(TAG, "Session $sessionId was erased on the server; dropping local data")
            discardLocal(app, sessionId)
            return@withLock Result(Outcome.DISCARDED)
        }
        val recordingId = row?.recordingId ?: (body?.get("recording_id") as? String)
        if (row != null) {
            if (row.recordingId == null && recordingId != null) sessionDao.markStarted(sessionId, recordingId)
            sessionDao.markFinished(sessionId)
        }
        Log.i(TAG, "Finished $sessionId via $source: $totalChunks chunks, ${voiceSeconds}s voice")
        Result(Outcome.FINISHED, recordingId, totalChunks)
    }

    /** Session erased on the server (heartbeat or finish said "discarded"): drop rows and files. */
    suspend fun discardLocal(context: Context, sessionId: String) {
        val db = AppDatabase.getInstance(context)
        db.sessionDao().markDiscarded(sessionId)
        db.chunkDao().deleteChunksForSession(sessionId)
        AudioChunkManager.sessionDir(context, sessionId).deleteRecursively()
    }

    fun finishWorkName(sessionId: String) = "finish_session_$sessionId"

    /** One unique FinishSessionWorker per session; KEEP never duplicates a pending one. */
    fun enqueueFinishWorker(context: Context, sessionId: String) {
        val request = OneTimeWorkRequestBuilder<FinishSessionWorker>()
            .setInputData(workDataOf(FinishSessionWorker.KEY_SESSION_ID to sessionId))
            .setConstraints(Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build())
            .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 30, TimeUnit.SECONDS)
            .build()
        WorkManager.getInstance(context.applicationContext)
            .enqueueUniqueWork(finishWorkName(sessionId), ExistingWorkPolicy.KEEP, request)
        Log.i(TAG, "FinishSessionWorker queued for $sessionId")
    }
}
