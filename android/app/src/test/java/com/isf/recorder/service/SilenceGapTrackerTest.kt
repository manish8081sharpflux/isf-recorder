package com.isf.recorder.service

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class SilenceGapTrackerTest {
    private val fs = VoiceGate.FRAME_SAMPLES.toDouble() / VoiceGate.SAMPLE_RATE   // 0.032 s

    /** Replays the real gate over levels and returns the tracker. */
    private fun replay(levels: List<Float>): SilenceGapTracker {
        val gate = VoiceGate<Int>()
        val t = SilenceGapTracker()
        levels.forEachIndexed { i, db -> t.onFrame(gate.onFrame(i, db).write.size, i * 32L) }
        t.breakRun()
        return t
    }

    @Test
    fun gapPlacedOnSavedTimelineAndPreRollNotCounted() {
        // 5 s speech, 60 s silence, 5 s speech
        val sp = VoiceGate.framesFor(5000); val sil = VoiceGate.framesFor(60_000)
        val t = replay(List(sp) { -20f } + List(sil) { -70f } + List(sp) { -20f })
        assertEquals(1, t.gaps.size)
        val g = t.gaps[0]
        // saved before the gap: speech + 2 s hangover; skipped: silence - hangover - 1 s pre-roll (+ start frames)
        val savedBefore = (sp + VoiceGate.framesFor(VoiceGate.HANGOVER_MS)) * fs
        assertEquals(savedBefore, g.atVoiceSec, 0.1)
        assertEquals(60.0 - 2.0 - 1.0, g.seconds, 0.2)
    }

    @Test
    fun shortGapsIgnoredAndTrailingSilenceKept() {
        val t = SilenceGapTracker()
        repeat(10) { t.onFrame(1, 0) }
        repeat(50) { assertNull(t.onFrame(0, 0)) }      // 1.6 s
        assertNull(t.onFrame(1, 0))
        repeat(200) { t.onFrame(0, 1000) }               // 6.4 s at the end
        val g = t.breakRun()!!
        assertEquals(11 * fs, g.atVoiceSec, 1e-6)
        assertEquals(200 * fs, g.seconds, 1e-6)
        assertEquals(1, t.gaps.size)
    }

    @Test
    fun capAndJson() {
        val t = SilenceGapTracker(maxGaps = 2)
        repeat(3) { repeat(200) { t.onFrame(0, 5) }; t.onFrame(1, 6) }
        assertEquals(2, t.gaps.size)
        val json = SilenceGapTracker.toJson(t.gaps)
        assertTrue(json.startsWith("[{") && json.contains("\"at_voice_sec\"") && json.contains("\"seconds\":6.4"))
    }
}
