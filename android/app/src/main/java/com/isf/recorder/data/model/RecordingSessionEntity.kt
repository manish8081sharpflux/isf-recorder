package com.isf.recorder.data.model

import androidx.room.Entity
import androidx.room.PrimaryKey

/**
 * One recording session persisted at start, so finish/retry and orphan recovery work after the
 * process dies or while offline. Added in DB version 2 (hand-written MIGRATION_1_2).
 */
@Entity(tableName = "recording_sessions")
data class RecordingSessionEntity(
    @PrimaryKey val sessionId: String,
    val state: String,                       // SessionState.*
    val startedAtMs: Long,
    val recordingId: String? = null,         // server UUID once /start succeeded
    val micSource: String = "built_in",
    val mode: String? = null,
    val balgruhName: String? = null,
    val assistantCoach: String? = null,
    val participantsJson: String? = null,
    val title: String? = null,
    val startSynced: Boolean = false,        // /start (with metadata) reached the server
    val startAttempts: Int = 0,              // non-network /start failures
    val totalChunks: Int = 0,
    val voiceSeconds: Int = 0,
    val sessionSeconds: Int = 0,
    val silenceSkippedSeconds: Int = 0,
    val stoppedAtMs: Long? = null,
    val finishedAtMs: Long? = null,
    val updatedAtMs: Long = System.currentTimeMillis(),
    val assistantCoachesJson: String? = null, // v3: JSON array [{user_id?, name}]
    val silenceGapsJson: String? = null       // v4: [{at_voice_sec, wall_start_ms, seconds}] sent with /finish
)

object SessionState {
    const val ACTIVE = "ACTIVE"         // recording in progress (or orphaned if no service owns it)
    const val STOPPED = "STOPPED"       // capture ended; chunks may still be uploading, /finish pending
    const val FINISHED = "FINISHED"     // /finish succeeded
    const val DISCARDED = "DISCARDED"   // erased on the server; nothing more to send
}

/** Display title rule shared with the server (/start): schedule → "<Balgruh> · <assistant coach names>". */
fun sessionDisplayTitle(mode: String?, balgruhName: String?, title: String?, coachNames: String? = null): String? = when {
    !title.isNullOrBlank() -> title.trim()
    mode == "schedule" && !balgruhName.isNullOrBlank() ->
        "${balgruhName.trim()} · ${coachNames?.trim()?.takeIf { it.isNotEmpty() } ?: "Daily schedule"}"
    else -> null
}
