package com.isf.recorder.sync

import android.content.Context
import android.util.Log
import com.isf.recorder.data.api.ISFApiClient
import com.isf.recorder.data.db.AppDatabase
import com.isf.recorder.data.model.SessionState
import com.isf.recorder.service.AudioRecordingService
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import java.io.IOException
import java.util.concurrent.ConcurrentHashMap

/**
 * Makes sure POST /recordings/start (with the persisted mode/Balgruh/coach/participants/title) reached
 * the server before any chunk or /finish is sent — otherwise the server auto-creates a legacy row
 * without metadata. Safe to call from many places: one call per session at a time, idempotent server.
 */
object SessionStarter {
    private const val TAG = "SessionStarter"
    /** After this many non-network failures (e.g. 4xx) uploads go ahead without metadata. */
    const val MAX_HTTP_START_FAILURES = 3

    private val locks = ConcurrentHashMap<String, Mutex>()

    /** @return true when chunks/finish may be sent for [sessionId]. */
    suspend fun ensureStarted(context: Context, sessionId: String): Boolean {
        val dao = AppDatabase.getInstance(context).sessionDao()
        val first = dao.get(sessionId) ?: return true          // legacy v1 session: no metadata to send
        if (first.startSynced || first.state == SessionState.DISCARDED) return true
        return locks.getOrPut(sessionId) { Mutex() }.withLock {
            val row = dao.get(sessionId) ?: return@withLock true
            if (row.startSynced) return@withLock true
            val result = ISFApiClient(context).startRecording(
                row.sessionId, row.micSource,
                ISFApiClient.StartMetadata(
                    mode = row.mode,
                    balgruhName = row.balgruhName,
                    assistantCoach = row.assistantCoach,
                    assistantCoachesJson = row.assistantCoachesJson,
                    participantsJson = row.participantsJson,
                    title = row.title
                )
            )
            if (result.isSuccess) {
                val recordingId = result.getOrNull()?.takeIf { it.isNotBlank() }
                dao.markStarted(sessionId, recordingId)
                AudioRecordingService.onRecordingIdKnown(sessionId, recordingId)
                Log.i(TAG, "Session $sessionId started on server: recording_id=$recordingId")
                true
            } else {
                val e = result.exceptionOrNull()
                if (e is IOException) {
                    Log.w(TAG, "/start for $sessionId failed (network): ${e.message}")
                    false
                } else {
                    dao.incrementStartAttempts(sessionId)
                    val attempts = row.startAttempts + 1
                    Log.e(TAG, "/start for $sessionId rejected ($attempts/$MAX_HTTP_START_FAILURES): ${e?.message}")
                    if (attempts >= MAX_HTTP_START_FAILURES) {
                        // Never block the audio forever: the chunk endpoint auto-creates the recording.
                        dao.markStarted(sessionId, null)
                        true
                    } else false
                }
            }
        }
    }
}
