package com.isf.recorder.service

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import kotlin.math.PI
import kotlin.math.sin

class SpeechBandFilterTest {
    private fun tone(hz: Double, dbfs: Double, frames: Int = 40): List<ShortArray> {
        val amp = Math.pow(10.0, dbfs / 20.0) * Math.sqrt(2.0) * 32767   // sine RMS = amp / sqrt(2)
        var n = 0
        return List(frames) {
            ShortArray(VoiceGate.FRAME_SAMPLES) { (amp * sin(2 * PI * hz * (n++) / VoiceGate.SAMPLE_RATE)).toInt().toShort() }
        }
    }

    /** Level of the last frame (the filter has settled). */
    private fun settledDbfs(frames: List<ShortArray>): Float {
        val f = SpeechBandFilter()
        var last = Float.NEGATIVE_INFINITY
        frames.forEach { last = f.bandDbfs(it) }
        return last
    }

    @Test
    fun humBelow80HzIsCut() {
        // a loud 50 Hz electrical hum at -20 dBFS drops by ~16 dB in the speech band
        assertTrue(settledDbfs(tone(50.0, -20.0)) < -34f)
        // 30 Hz rumble almost disappears
        assertTrue(settledDbfs(tone(30.0, -20.0)) < -50f)
        // a deep male voice at 100 Hz loses only about 1 dB
        assertTrue(settledDbfs(tone(100.0, -20.0)) > -22f)
    }

    @Test
    fun speechFrequenciesPassUnchanged() {
        assertEquals(-20f, settledDbfs(tone(300.0, -20.0)), 0.5f)    // child's voice
        assertEquals(-20f, settledDbfs(tone(1000.0, -20.0)), 0.5f)
        assertEquals(-20f, settledDbfs(tone(4000.0, -20.0)), 0.5f)
    }

    @Test
    fun digitalSilenceStaysInfinite() {
        assertEquals(Float.NEGATIVE_INFINITY, SpeechBandFilter().bandDbfs(ShortArray(VoiceGate.FRAME_SAMPLES)))
    }

    @Test
    fun minus40IsTheMinimumForVoice() {
        val gate = VoiceGate<Int>()
        // quiet room: the floor settles low, so before 21 Oct -45 dBFS would have counted as voice
        repeat(400) { gate.onFrame(it, -70f) }
        assertFalse(gate.isVoiceFrame(-45f))
        assertTrue(gate.isVoiceFrame(-39f))
        assertTrue(gate.isVoiceFrame(-20f))
    }
}
