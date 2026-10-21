package com.isf.recorder.sync

import android.content.Context
import android.util.Log
import androidx.work.*
import com.isf.recorder.data.api.ISFApiClient
import com.isf.recorder.data.db.AppDatabase
import com.isf.recorder.data.model.AudioChunkEntity
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import java.io.File
import java.util.concurrent.TimeUnit

/**
 * Uploads chunks. Every upload of a given chunk goes through one process-wide lock and re-checks
 * Room inside it, so the realtime path, the Stop path and the workers never send the same chunk
 * twice at the same time (bug I).
 */
class ChunkUploader(context: Context) {
    private val appContext = context.applicationContext
    private val apiClient = ISFApiClient(appContext)
    private val chunkDao = AppDatabase.getInstance(appContext).chunkDao()
    private val networkMonitor = NetworkMonitor.getInstance(appContext)

    enum class UploadResult { UPLOADED, ALREADY_UPLOADED, FILE_MISSING, OFFLINE, FAILED }

    /** Realtime path: try now, fall back to the offline worker. */
    suspend fun uploadChunkImmediate(chunk: AudioChunkEntity) {
        when (upload(chunk)) {
            UploadResult.OFFLINE, UploadResult.FAILED -> scheduleOfflineSync()
            else -> Unit
        }
    }

    suspend fun upload(chunk: AudioChunkEntity): UploadResult = lockFor(chunk.sessionId, chunk.chunkIndex).withLock {
        val current = chunkDao.getChunk(chunk.sessionId, chunk.chunkIndex) ?: chunk
        if (current.isUploaded) return@withLock UploadResult.ALREADY_UPLOADED
        if (!networkMonitor.isCurrentlyOnline()) return@withLock UploadResult.OFFLINE

        val file = File(current.filePath)
        if (!file.exists()) {
            Log.w(TAG, "Chunk file missing, marking uploaded to avoid a retry loop: ${current.filePath}")
            chunkDao.markUploaded(current.id)
            return@withLock UploadResult.FILE_MISSING
        }
        // The server must know the session (with its metadata) before the first chunk.
        if (!SessionStarter.ensureStarted(appContext, current.sessionId)) return@withLock UploadResult.FAILED

        val result = apiClient.uploadChunk(
            sessionId = current.sessionId,
            chunkIndex = current.chunkIndex,
            file = file,
            durationSeconds = current.durationSeconds,
            checksum = current.checksum
        )
        if (result.isSuccess) {
            chunkDao.markUploaded(current.id)
            try { file.delete() } catch (e: Exception) { Log.w(TAG, "Could not delete local chunk: ${e.message}") }
            Log.i(TAG, "Uploaded chunk ${current.chunkIndex} of ${current.sessionId}")
            UploadResult.UPLOADED
        } else {
            Log.w(TAG, "Upload of chunk ${current.chunkIndex} failed: ${result.exceptionOrNull()?.message}")
            UploadResult.FAILED
        }
    }

    fun scheduleOfflineSync() = scheduleOfflineSync(appContext)

    companion object {
        private const val TAG = "ChunkUploader"
        const val OFFLINE_SYNC_WORK = "ISF_CHUNK_OFFLINE_SYNC"

        private val locks = Array(64) { Mutex() }
        private fun lockFor(sessionId: String, chunkIndex: Int): Mutex =
            locks[(sessionId.hashCode() * 31 + chunkIndex).mod(locks.size)]

        /** Background scope for realtime uploads (not tied to the service lifetime). */
        val uploadScope = CoroutineScope(SupervisorJob() + Dispatchers.IO)

        fun uploadInBackground(context: Context, chunk: AudioChunkEntity) {
            val app = context.applicationContext
            uploadScope.launch { ChunkUploader(app).uploadChunkImmediate(chunk) }
        }

        /**
         * KEEP (not REPLACE, bug I): a running worker is never duplicated. The worker loops until no
         * pending chunk is left, so chunks queued while it runs are still picked up.
         */
        fun scheduleOfflineSync(context: Context) {
            val request = OneTimeWorkRequestBuilder<OfflineSyncWorker>()
                .setConstraints(Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build())
                .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 10, TimeUnit.SECONDS)
                .build()
            WorkManager.getInstance(context.applicationContext)
                .enqueueUniqueWork(OFFLINE_SYNC_WORK, ExistingWorkPolicy.KEEP, request)
        }
    }
}
