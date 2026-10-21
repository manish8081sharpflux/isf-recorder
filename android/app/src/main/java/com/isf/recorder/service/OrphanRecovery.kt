package com.isf.recorder.service

import android.content.Context
import android.util.Log
import com.isf.recorder.data.db.AppDatabase
import com.isf.recorder.data.model.AudioChunkEntity
import com.isf.recorder.data.model.SessionState
import com.isf.recorder.sync.SessionFinisher
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlin.math.roundToInt

/**
 * Bug M: a session whose process died (Android 14 cannot restart a microphone FGS, so the service is
 * START_NOT_STICKY) is still ACTIVE in Room. Run from ISFApplication.onCreate and service onCreate:
 * repair + register the partial chunk, mark the session STOPPED, queue its FinishSessionWorker and
 * notify "Your recording from HH:MM was interrupted and has been saved".
 */
object OrphanRecovery {
    private const val TAG = "OrphanRecovery"
    private val mutex = Mutex()

    data class RepairResult(val registered: Int, val deleted: Int, val youngSkipped: Int)

    /**
     * Registers chunk files of [sessionId] that are on disk but not in Room (process died mid-chunk or
     * before the insert). Files modified after [notModifiedAfterMs] are left alone (may still be in use).
     */
    suspend fun registerOrphanFiles(context: Context, sessionId: String, notModifiedAfterMs: Long): RepairResult {
        val dir = AudioChunkManager.sessionDir(context, sessionId)
        val files = dir.listFiles()?.filter { AudioChunkManager.chunkIndexOf(it) != null } ?: return RepairResult(0, 0, 0)
        val chunkDao = AppDatabase.getInstance(context).chunkDao()
        var registered = 0
        var deleted = 0
        var young = 0
        for (file in files.sortedBy { AudioChunkManager.chunkIndexOf(it) }) {
            val index = AudioChunkManager.chunkIndexOf(file) ?: continue
            if (chunkDao.getChunk(sessionId, index) != null) continue
            if (file.lastModified() > notModifiedAfterMs) { young++; continue }
            val dataBytes = try { WavHeaderHelper.repairFile(file) } catch (e: Exception) { -1L }
            if (dataBytes <= 0) {
                file.delete()   // header-only / unreadable: never upload an empty chunk
                deleted++
                continue
            }
            chunkDao.insertChunk(
                AudioChunkEntity(
                    sessionId = sessionId,
                    chunkIndex = index,
                    filePath = file.absolutePath,
                    durationSeconds = dataBytes / AudioChunkManager.BYTES_PER_SECOND.toFloat(),
                    sizeBytes = file.length(),
                    checksum = AudioChunkManager.sha256(file),
                    isUploaded = false
                )
            )
            registered++
            Log.i(TAG, "Recovered chunk $index of $sessionId (${dataBytes} bytes)")
        }
        return RepairResult(registered, deleted, young)
    }

    /**
     * @param cutoffMs only sessions started (and files written) before this time are touched, so a
     *                 recording started after the process/service came up is never mistaken for an orphan.
     */
    suspend fun run(context: Context, cutoffMs: Long) = mutex.withLock {
        val app = context.applicationContext
        val db = AppDatabase.getInstance(app)
        val sessionDao = db.sessionDao()
        val chunkDao = db.chunkDao()

        // 1. ACTIVE sessions with no live service → interrupted.
        for (row in sessionDao.getByState(SessionState.ACTIVE)) {
            val sid = row.sessionId
            if (row.startedAtMs >= cutoffMs || AudioRecordingService.isLiveSession(sid)) continue
            try {
                registerOrphanFiles(app, sid, notModifiedAfterMs = cutoffMs)
                val total = (chunkDao.getMaxChunkIndex(sid) ?: -1) + 1
                val voice = chunkDao.getAllChunksForSession(sid).sumOf { it.durationSeconds.toDouble() }.roundToInt()
                sessionDao.updateProgress(sid, total, voice, row.sessionSeconds, row.silenceSkippedSeconds)
                if (sessionDao.markStopped(sid) == 1) {
                    SessionFinisher.enqueueFinishWorker(app, sid)
                    if (total > 0) RecorderNotifications.notifyInterrupted(app, sid, row.startedAtMs)
                    Log.w(TAG, "Recovered interrupted session $sid: $total chunks, ${voice}s voice")
                }
            } catch (t: Throwable) {
                Log.e(TAG, "Recovering $sid failed: ${t.message}", t)
            }
        }

        // 2. STOPPED sessions must have a finish worker (KEEP → no duplicate).
        for (row in sessionDao.getByState(SessionState.STOPPED)) {
            SessionFinisher.enqueueFinishWorker(app, row.sessionId)
        }

        // 3. v1.2.0 leftovers (no session row): register partial chunks and finish like the old app did.
        val dirs = AudioChunkManager.sessionsRoot(app).listFiles()?.filter { it.isDirectory } ?: emptyList()
        for (dir in dirs) {
            val sid = dir.name
            if (AudioRecordingService.isLiveSession(sid)) continue
            try {
                val row = sessionDao.get(sid)
                if (row == null) {
                    val r = registerOrphanFiles(app, sid, notModifiedAfterMs = cutoffMs)
                    if (r.registered > 0) SessionFinisher.enqueueFinishWorker(app, sid)
                }
                if (dir.listFiles().isNullOrEmpty() && dir.lastModified() < cutoffMs &&
                    (row == null || row.state == SessionState.FINISHED || row.state == SessionState.DISCARDED)
                ) dir.delete()
            } catch (t: Throwable) {
                Log.e(TAG, "Scanning $sid failed: ${t.message}", t)
            }
        }
    }
}
