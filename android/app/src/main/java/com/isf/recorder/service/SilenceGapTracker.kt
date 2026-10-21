package com.isf.recorder.service

import com.google.gson.Gson

/**
 * Records each stretch of skipped silence (pure, unit-tested; owned by the capture thread).
 * [onFrame] is called once per frame given to the [VoiceGate] with the number of frames the gate
 * wrote for it (0 = skipped; pre-roll flushes write several older frames at once). A gap is the run of
 * frames heard but never written; it is kept when it lasts >= [MIN_GAP_SECONDS] and is placed at its
 * position in the SAVED audio timeline (at_voice_sec) — the timeline the transcript uses.
 */
class SilenceGapTracker(
    private val frameSeconds: Double = VoiceGate.FRAME_SAMPLES.toDouble() / VoiceGate.SAMPLE_RATE,
    private val minGapSeconds: Double = MIN_GAP_SECONDS,
    private val maxGaps: Int = MAX_GAPS
) {
    data class Gap(val atVoiceSec: Double, val wallStartMs: Long, val seconds: Double)

    companion object {
        const val MIN_GAP_SECONDS = 3.0
        const val MAX_GAPS = 500

        /** The /finish payload shape: [{at_voice_sec, wall_start_ms, seconds}]. */
        fun toJson(gaps: List<Gap>): String = Gson().toJson(gaps.map { g ->
            linkedMapOf("at_voice_sec" to Math.round(g.atVoiceSec * 100) / 100.0,
                "wall_start_ms" to g.wallStartMs, "seconds" to Math.round(g.seconds * 10) / 10.0)
        })
    }

    private val list = ArrayList<Gap>()
    private var writtenFrames = 0L     // frames in the saved audio so far
    private var unwritten = 0L         // frames heard since the last write
    private var runStartMs = 0L

    val gaps: List<Gap> get() = list.toList()

    /** @return the gap closed by this frame (a write after a long skip), or null. */
    fun onFrame(framesWritten: Int, nowMs: Long): Gap? {
        if (unwritten == 0L) runStartMs = nowMs
        unwritten++
        if (framesWritten <= 0) return null
        // the written frames are the newest ones (pre-roll + this frame); the rest of the run was skipped
        val skipped = unwritten - framesWritten
        val gap = record(skipped)
        writtenFrames += framesWritten
        unwritten = 0
        return gap
    }

    /** The run of unwritten frames ends without a write (pause, mic switch, Stop). */
    fun breakRun(): Gap? {
        val gap = record(unwritten)
        unwritten = 0
        return gap
    }

    private fun record(skippedFrames: Long): Gap? {
        val seconds = skippedFrames * frameSeconds
        if (seconds < minGapSeconds || list.size >= maxGaps) return null
        return Gap(writtenFrames * frameSeconds, runStartMs, seconds).also { list.add(it) }
    }
}
