package com.isf.recorder.service

import kotlin.math.ceil
import kotlin.math.log10
import kotlin.math.sqrt

/**
 * Pure frame-by-frame silence gate (no Android dependencies, unit-tested).
 *
 * Feed every 512-sample frame with its RMS level in dBFS (loudness-only gate; no on-phone model).
 * The gate decides which frames are written to the chunk file:
 *  - While listening (no voice) frames go into a 1 s pre-roll ring buffer and are NOT written.
 *  - [START_FRAMES] consecutive voice frames start speech: the whole pre-roll (which includes those
 *    frames) is written, so the first word is never cut.
 *  - While speaking every frame is written; speech ends after [HANGOVER_MS] of silence (hangover
 *    frames are written too).
 *  - After [CLOSE_CHUNK_AFTER_SILENCE_MS] of silence the current chunk is closed once (so it is
 *    uploaded); the next speech opens a new chunk lazily. A close is only requested when audio was
 *    written since the last close, so no empty chunks are ever produced.
 *
 * Voice rule (Round 3, adaptive; 21 Oct: Bhavnesh's limits): the caller measures rms in the speech band
 * 80 – 8,000 Hz ([SpeechBandFilter]). A frame is VOICE if rms >= [MIN_VOICE_DBFS] AND (rms > [ALWAYS_VOICE_DBFS]
 * OR rms > noise floor + [FLOOR_MARGIN_DB]), otherwise SILENT. The floor is tracked by [NoiseFloorTracker]
 * (10th percentile of the last 10 s, recomputed every second; [WARMUP_FLOOR_DBFS] for the first 3 s).
 * Why: a fixed -45 dBFS threshold saved everything on a phone whose silent room measures a steady
 * -41 dBFS. Validated offline on 4 real Balgruh/meeting recordings (0% audible speech lost).
 *
 * Not thread-safe: owned by the capture thread.
 */
class VoiceGate<T>(private val config: Config = Config()) {

    data class Config(
        val alwaysVoiceDbfs: Float = ALWAYS_VOICE_DBFS,
        val minVoiceDbfs: Float = MIN_VOICE_DBFS,
        val floorMarginDb: Float = FLOOR_MARGIN_DB,
        val startFrames: Int = START_FRAMES,
        val preRollFrames: Int = framesFor(PRE_ROLL_MS),
        val hangoverFrames: Int = framesFor(HANGOVER_MS),
        val closeChunkAfterFrames: Int = framesFor(CLOSE_CHUNK_AFTER_SILENCE_MS)
    )

    /**
     * Result for one frame.
     * @param write frames to append to the chunk, in order (empty = skip)
     * @param closeChunk close + upload the current chunk now (after writing [write])
     * @param voiceActive gate state after this frame
     * @param voiceChanged true when [voiceActive] flipped on this frame
     */
    class Decision<T>(
        val write: List<T>,
        val closeChunk: Boolean,
        val voiceActive: Boolean,
        val voiceChanged: Boolean
    )

    companion object {
        // ── Tuning: the ONLY place these values live ──
        const val ALWAYS_VOICE_DBFS = -30f      // rms > this is always voice (noisy rooms are never cut)
        const val MIN_VOICE_DBFS = -40f         // Bhavnesh (21 Oct): quieter than this (speech band) is never voice
        const val FLOOR_MARGIN_DB = 6f          // ... or rms > noise floor + this
        const val FLOOR_WINDOW_MS = 10_000      // noise floor = percentile of the last 10 s
        const val FLOOR_PERCENTILE = 10         // 10th percentile
        const val FLOOR_UPDATE_MS = 1000        // recomputed once per second
        const val FLOOR_WARMUP_MS = 3000        // until 3 s of audio exist ...
        const val WARMUP_FLOOR_DBFS = -60f      // ... the floor is -60 dBFS, so the first words are never cut
        const val DIGITAL_SILENCE_DBFS = -90f   // frames at/below this (OS-muted mic) never define the floor
        const val START_FRAMES = 3              // consecutive voice frames to start speech
        const val PRE_ROLL_MS = 1000
        const val HANGOVER_MS = 2000
        const val CLOSE_CHUNK_AFTER_SILENCE_MS = 10_000

        const val SAMPLE_RATE = 16000
        const val FRAME_SAMPLES = 512           // 32 ms @ 16 kHz 
        const val FRAME_MS = FRAME_SAMPLES * 1000.0 / SAMPLE_RATE

        /** Number of whole frames covering [ms] (rounded up). */
        fun framesFor(ms: Int): Int = ceil(ms / FRAME_MS).toInt()

        /** RMS level of 16-bit PCM in dBFS: 20*log10(sqrt(mean(x^2))), x in [-1,1]. Digital silence = -inf. */
        fun rmsDbfs(frame: ShortArray, count: Int = frame.size): Float {
            if (count <= 0) return Float.NEGATIVE_INFINITY
            var sum = 0.0
            for (i in 0 until count) {
                val x = frame[i] / 32768.0
                sum += x * x
            }
            val rms = sqrt(sum / count)
            return if (rms <= 0.0) Float.NEGATIVE_INFINITY else (20.0 * log10(rms)).toFloat()
        }
    }

    private val floor = NoiseFloorTracker()
    private val preRoll = ArrayDeque<T>(config.preRollFrames + 1)
    private var speechRun = 0          // consecutive voice frames while listening
    private var silenceRun = 0         // frames since the end-of-speech run started (0 = none)
    private var unclosedAudio = false  // audio written since the last chunk close

    var voiceActive = false
        private set

    /** Current noise floor estimate in dBFS. */
    val noiseFloorDbfs: Float get() = floor.floorDbfs

    /** Voice rule against the current floor (does not update the floor). */
    fun isVoiceFrame(dbfs: Float): Boolean =
        dbfs >= config.minVoiceDbfs &&
            (dbfs > config.alwaysVoiceDbfs || dbfs > floor.floorDbfs + config.floorMarginDb)

    fun onFrame(frame: T, dbfs: Float): Decision<T> {
        floor.add(dbfs)
        val voice = isVoiceFrame(dbfs)
        if (voiceActive) {
            if (voice) {
                silenceRun = 0
            } else {
                silenceRun++
            }
            unclosedAudio = true
            if (silenceRun >= config.hangoverFrames) {
                voiceActive = false
                speechRun = 0
                return Decision(listOf(frame), closeChunk = false, voiceActive = false, voiceChanged = true)
            }
            return Decision(listOf(frame), closeChunk = false, voiceActive = true, voiceChanged = false)
        }

        // Listening: buffer in pre-roll, look for a speech start.
        preRoll.addLast(frame)
        while (preRoll.size > config.preRollFrames) preRoll.removeFirst()
        speechRun = if (voice) speechRun + 1 else 0

        if (speechRun >= config.startFrames) {
            voiceActive = true
            silenceRun = 0
            speechRun = 0
            unclosedAudio = true
            val out = preRoll.toList()
            preRoll.clear()
            return Decision(out, closeChunk = false, voiceActive = true, voiceChanged = true)
        }

        if (silenceRun > 0) silenceRun++
        if (unclosedAudio && silenceRun >= config.closeChunkAfterFrames) {
            unclosedAudio = false
            return Decision(emptyList(), closeChunk = true, voiceActive = false, voiceChanged = false)
        }
        return Decision(emptyList(), closeChunk = false, voiceActive = false, voiceChanged = false)
    }

    /** Frames currently held in the pre-roll (not yet written). */
    val pendingFrames: Int get() = preRoll.size

    /** Call when the chunk was closed for another reason (pause, rotation) or audio is discontinuous.
     *  The noise floor is kept: the room has not changed. */
    fun reset() {
        preRoll.clear()
        speechRun = 0
        silenceRun = 0
        unclosedAudio = false
        voiceActive = false
    }
}

/**
 * Adaptive noise floor (pure, unit-tested). Keeps the dBFS of the last [FLOOR_WINDOW_MS] of frames and
 * recomputes the floor every [FLOOR_UPDATE_MS] as their [FLOOR_PERCENTILE]th percentile. Until
 * [FLOOR_WARMUP_MS] of audio has been seen the floor is [WARMUP_FLOOR_DBFS]. Digital-silence frames
 * (<= [DIGITAL_SILENCE_DBFS], e.g. -inf) are counted as audio but never pull the floor down; if the
 * whole window is digital silence the warm-up floor is used.
 */
class NoiseFloorTracker(
    private val windowFrames: Int = VoiceGate.framesFor(VoiceGate.FLOOR_WINDOW_MS),
    private val updateFrames: Int = VoiceGate.framesFor(VoiceGate.FLOOR_UPDATE_MS),
    private val warmupFrames: Int = VoiceGate.framesFor(VoiceGate.FLOOR_WARMUP_MS),
    private val percentile: Int = VoiceGate.FLOOR_PERCENTILE,
    private val defaultFloor: Float = VoiceGate.WARMUP_FLOOR_DBFS,
    private val digitalSilence: Float = VoiceGate.DIGITAL_SILENCE_DBFS
) {
    private val ring = FloatArray(windowFrames)
    private var next = 0
    private var filled = 0
    private var seen = 0L
    private var sinceUpdate = 0

    var floorDbfs: Float = defaultFloor
        private set

    fun add(dbfs: Float) {
        ring[next] = dbfs
        next = (next + 1) % windowFrames
        if (filled < windowFrames) filled++
        seen++
        sinceUpdate++
        if (seen < warmupFrames) return
        if (seen == warmupFrames.toLong() || sinceUpdate >= updateFrames) {
            sinceUpdate = 0
            floorDbfs = compute()
        }
    }

    private fun compute(): Float {
        val vals = FloatArray(filled)
        var n = 0
        for (i in 0 until filled) {
            val v = ring[i]
            if (v > digitalSilence) vals[n++] = v
        }
        if (n == 0) return defaultFloor
        vals.sort(0, n)
        val idx = ((n - 1) * percentile / 100.0).toInt().coerceIn(0, n - 1)
        return vals[idx]
    }
}
