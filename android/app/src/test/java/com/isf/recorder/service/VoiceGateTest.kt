package com.isf.recorder.service

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class VoiceGateTest {

    private val quiet = -60f          // the warm-up floor itself: silent
    private val loud = -25f           // above ALWAYS_VOICE_DBFS

    private fun gate() = VoiceGate<Int>()

    /** Feeds frames [from, from+count) at [dbfs]; returns frames written + number of chunk closes. */
    private fun VoiceGate<Int>.feed(from: Int, count: Int, dbfs: Float): Pair<List<Int>, Int> {
        val written = mutableListOf<Int>()
        var closes = 0
        for (i in from until from + count) {
            val d = onFrame(i, dbfs)
            written += d.write
            if (d.closeChunk) closes++
        }
        return written to closes
    }

    @Test
    fun constants() {
        assertEquals(-30f, VoiceGate.ALWAYS_VOICE_DBFS)
        assertEquals(6f, VoiceGate.FLOOR_MARGIN_DB)
        assertEquals(-60f, VoiceGate.WARMUP_FLOOR_DBFS)
        assertEquals(10_000, VoiceGate.FLOOR_WINDOW_MS)
        assertEquals(10, VoiceGate.FLOOR_PERCENTILE)
        assertEquals(3, VoiceGate.START_FRAMES)
        assertEquals(32, VoiceGate.framesFor(1000))     // 1.0 s pre-roll
        assertEquals(63, VoiceGate.framesFor(2000))     // 2.0 s hangover
        assertEquals(313, VoiceGate.framesFor(10_000))  // 10 s silence before closing a chunk
    }

    @Test
    fun loudnessThresholdBoundary() {
        val g = gate()                          // warm-up floor -60 -> threshold -54
        assertFalse(g.isVoiceFrame(-54f))
        assertTrue(g.isVoiceFrame(-53.9f))
        assertFalse(g.isVoiceFrame(Float.NEGATIVE_INFINITY))
        g.feed(0, 200, -41f)                    // floor adapts to -41 -> threshold -35
        assertEquals(-41f, g.noiseFloorDbfs, 0.001f)
        assertFalse(g.isVoiceFrame(-35f))
        assertTrue(g.isVoiceFrame(-34.9f))
        assertTrue(g.isVoiceFrame(-29.9f))
    }

    @Test
    fun silenceIsNeverWrittenAndNoChunkIsClosed() {
        val g = gate()
        val (written, closes) = g.feed(0, 2000, quiet)
        assertTrue(written.isEmpty())
        assertEquals(0, closes)
        assertFalse(g.voiceActive)
    }

    @Test
    fun speechStartsAfterThreeLoudFramesAndWritesPreRollInOrder() {
        val g = gate()
        g.feed(0, 100, quiet)
        val (w2, _) = g.feed(100, 2, loud)
        assertTrue(w2.isEmpty())
        val d = g.onFrame(102, loud)
        assertTrue(d.voiceActive && d.voiceChanged)
        assertEquals((71..102).toList(), d.write)
        assertEquals(listOf(103), g.onFrame(103, loud).write)
    }

    @Test
    fun interruptedRunDoesNotStartSpeech() {
        val g = gate()
        g.feed(0, 2, loud)
        g.feed(2, 1, quiet)
        val (w, _) = g.feed(3, 2, loud)
        assertTrue(w.isEmpty())
        assertFalse(g.voiceActive)
    }

    @Test
    fun hangoverKeepsWritingTwoSecondsThenStops() {
        val g = gate()
        g.feed(0, 3, loud)
        val (hang, _) = g.feed(3, 63, quiet)
        assertEquals((3 until 66).toList(), hang)
        assertFalse(g.voiceActive)
        assertTrue(g.feed(66, 10, quiet).first.isEmpty())
    }

    @Test
    fun loudFrameDuringHangoverResetsIt() {
        val g = gate()
        g.feed(0, 3, loud)
        g.feed(3, 60, quiet)
        g.feed(63, 1, loud)
        val (w, _) = g.feed(64, 62, quiet)
        assertEquals(62, w.size)
        assertTrue(g.voiceActive)
    }

    @Test
    fun noisyRoomNeverEndsSpeech() {
        val g = gate()
        g.feed(0, 3, loud)
        g.feed(3, 5000, -24f)
        assertTrue(g.voiceActive)
    }

    // ── Adaptive floor scenarios (Round 3) ──

    private fun VoiceGate<Int>.feedLevels(levels: List<Float>): Set<Int> {
        val written = mutableSetOf<Int>()
        levels.forEachIndexed { i, db -> written += onFrame(i, db).write }
        return written
    }

    @Test
    fun steadyRoomFloorKeepsOnlySpeech() {
        // 30 s of a steady -41 dBFS room, 5 s of speech at -30, 30 s of room again
        val room = 938; val speech = 156
        val levels = List(room) { -41f + (it % 3) * 0.3f } + List(speech) { -30f } + List(room) { -41f + (it % 3) * 0.3f }
        val w = gate().feedLevels(levels)
        val speechRange = room until room + speech
        assertTrue(speechRange.all { it in w })                       // all speech kept
        val pre = VoiceGate.framesFor(1000); val hang = VoiceGate.framesFor(2000)
        // silence well after warm-up and outside pre-roll/hangover is never kept
        assertTrue((300 until room - pre).none { it in w })
        assertTrue((room + speech + hang + 1 until levels.size).none { it in w })
    }

    @Test
    fun noisyRoomKeepsEverything() {
        val levels = List(3000) { -24f + (it % 5) * 0.5f }
        assertEquals(3000, gate().feedLevels(levels).size)
    }

    @Test
    fun firstThreeSecondsAreNeverCut() {
        // a coach starts talking softly (-50 dBFS) right away: the warm-up floor (-60) keeps it
        val warm = VoiceGate.framesFor(VoiceGate.FLOOR_WARMUP_MS)
        val w = gate().feedLevels(List(warm) { -50f })
        assertEquals(warm, w.size)
    }

    @Test
    fun chunkClosedOnceAfterTenSecondsOfSilence() {
        val g = gate()
        g.feed(0, 3, loud)
        assertEquals(0, g.feed(3, 312, quiet).second)
        val d = g.onFrame(315, quiet)
        assertTrue(d.closeChunk)
        assertTrue(d.write.isEmpty())
        assertEquals(0, g.feed(316, 5000, quiet).second)
    }

    @Test
    fun newSpeechAfterCloseCanCloseAgain() {
        val g = gate()
        g.feed(0, 3, loud)
        assertEquals(1, g.feed(3, 400, quiet).second)
        assertEquals(32, g.feed(403, 3, loud).first.size)
        assertEquals(1, g.feed(406, 400, quiet).second)
    }

    @Test
    fun digitalZerosAreSkipped() {
        val g = gate()
        val zeros = VoiceGate.rmsDbfs(ShortArray(512))
        repeat(100) { assertTrue(g.onFrame(it, zeros).write.isEmpty()) }
    }

    @Test
    fun rmsDbfsMatchesFormula() {
        assertEquals(Float.NEGATIVE_INFINITY, VoiceGate.rmsDbfs(ShortArray(512)), 0f)
        val sq = ShortArray(512) { if (it % 2 == 0) 32767 else -32768 }
        assertEquals(0f, VoiceGate.rmsDbfs(sq), 0.01f)
        val c = ShortArray(512) { 328 }   // 0.01 → -40 dBFS
        assertEquals(-40f, VoiceGate.rmsDbfs(c), 0.05f)
    }

    @Test
    fun resetDropsPreRollAndState() {
        val g = gate()
        g.feed(0, 2, loud)
        g.reset()
        assertEquals(0, g.pendingFrames)
        assertTrue(g.feed(2, 1, loud).first.isEmpty())
        g.feed(3, 3, loud)
        assertTrue(g.voiceActive)
        g.reset()
        assertFalse(g.voiceActive)
        assertEquals(0, g.feed(10, 1000, quiet).second)
    }

    @Test
    fun floorTrackerWarmupPercentileAndUpdateRate() {
        val t = NoiseFloorTracker()
        val warm = VoiceGate.framesFor(VoiceGate.FLOOR_WARMUP_MS)
        repeat(warm - 1) { t.add(-41f) }
        assertEquals(-60f, t.floorDbfs)                              // still warming up
        t.add(-41f)
        assertEquals(-41f, t.floorDbfs)                              // first estimate at 3 s
        // 10th percentile: a mostly-loud window (80 % loud) keeps the quiet floor
        repeat(250) { t.add(-20f) }
        assertEquals(-41f, t.floorDbfs)
        // recomputed only once per second
        val t2 = NoiseFloorTracker()
        repeat(warm) { t2.add(-41f) }
        repeat(VoiceGate.framesFor(VoiceGate.FLOOR_WINDOW_MS)) { t2.add(-50f) }
        assertEquals(-50f, t2.floorDbfs)
        t2.add(-70f)
        assertEquals(-50f, t2.floorDbfs)                             // 1 quiet frame does not move it
        repeat(2 * VoiceGate.framesFor(1000)) { t2.add(-70f) }
        assertEquals(-70f, t2.floorDbfs)                             // > 10 % of the window, picked up within 1 s
    }

    @Test
    fun digitalSilenceNeverDefinesTheFloor() {
        val t = NoiseFloorTracker()
        repeat(1000) { t.add(Float.NEGATIVE_INFINITY) }
        assertEquals(-60f, t.floorDbfs)
        repeat(1000) { t.add(-41f) }
        assertEquals(-41f, t.floorDbfs)
    }
}
