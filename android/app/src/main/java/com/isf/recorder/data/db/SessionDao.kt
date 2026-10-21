package com.isf.recorder.data.db

import androidx.room.Dao
import androidx.room.Insert
import androidx.room.OnConflictStrategy
import androidx.room.Query
import com.isf.recorder.data.model.RecordingSessionEntity

@Dao
interface SessionDao {
    @Insert(onConflict = OnConflictStrategy.IGNORE)
    suspend fun insert(session: RecordingSessionEntity): Long

    @Query("SELECT * FROM recording_sessions WHERE sessionId = :sessionId")
    suspend fun get(sessionId: String): RecordingSessionEntity?

    @Query("SELECT * FROM recording_sessions WHERE state = :state ORDER BY startedAtMs ASC")
    suspend fun getByState(state: String): List<RecordingSessionEntity>

    @Query("UPDATE recording_sessions SET recordingId = :recordingId, startSynced = 1, updatedAtMs = :now WHERE sessionId = :sessionId")
    suspend fun markStarted(sessionId: String, recordingId: String?, now: Long = System.currentTimeMillis())

    @Query("UPDATE recording_sessions SET startAttempts = startAttempts + 1, updatedAtMs = :now WHERE sessionId = :sessionId")
    suspend fun incrementStartAttempts(sessionId: String, now: Long = System.currentTimeMillis())

    @Query("""UPDATE recording_sessions SET totalChunks = :totalChunks, voiceSeconds = :voiceSeconds,
              sessionSeconds = :sessionSeconds, silenceSkippedSeconds = :silenceSkipped, updatedAtMs = :now
              WHERE sessionId = :sessionId""")
    suspend fun updateProgress(
        sessionId: String, totalChunks: Int, voiceSeconds: Int, sessionSeconds: Int,
        silenceSkipped: Int, now: Long = System.currentTimeMillis()
    )

    @Query("UPDATE recording_sessions SET silenceGapsJson = :json, updatedAtMs = :now WHERE sessionId = :sessionId")
    suspend fun updateSilenceGaps(sessionId: String, json: String, now: Long = System.currentTimeMillis())

    /** ACTIVE → STOPPED. Returns rows changed (0 = someone else already stopped it). */
    @Query("""UPDATE recording_sessions SET state = 'STOPPED', stoppedAtMs = :now, updatedAtMs = :now
              WHERE sessionId = :sessionId AND state = 'ACTIVE'""")
    suspend fun markStopped(sessionId: String, now: Long = System.currentTimeMillis()): Int

    @Query("UPDATE recording_sessions SET state = 'FINISHED', finishedAtMs = :now, updatedAtMs = :now WHERE sessionId = :sessionId")
    suspend fun markFinished(sessionId: String, now: Long = System.currentTimeMillis())

    @Query("UPDATE recording_sessions SET state = 'DISCARDED', updatedAtMs = :now WHERE sessionId = :sessionId")
    suspend fun markDiscarded(sessionId: String, now: Long = System.currentTimeMillis())
}
