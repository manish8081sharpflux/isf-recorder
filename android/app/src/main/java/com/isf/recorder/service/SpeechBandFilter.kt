package com.isf.recorder.service

import kotlin.math.PI
import kotlin.math.cos
import kotlin.math.log10
import kotlin.math.sin
import kotlin.math.sqrt

/**
 * Speech band for voice detection (21 Oct, Bhavnesh's Stage 1): only 80 – 8,000 Hz counts when measuring how
 * loud a frame is, so fan / AC hum, electrical buzz and table thumps below 80 Hz no longer look like voice.
 * Used ONLY for the voice decision — the saved audio is never filtered.
 *
 * 4th-order Butterworth high-pass at [LOW_HZ] (two RBJ biquads): 50 Hz hum drops ~16 dB, 100 Hz speech ~1 dB. The 8,000 Hz top is the Nyquist limit of the
 * 16 kHz recording, so a low-pass is only added when the sample rate is above 2 × [HIGH_HZ].
 * State carries over between frames (no click at frame edges). Pure Kotlin, unit-tested; capture thread only.
 */
class SpeechBandFilter(sampleRate: Int = VoiceGate.SAMPLE_RATE) {
    private val highPass1 = Biquad.highPass(sampleRate, LOW_HZ, 0.5411961)
    private val highPass2 = Biquad.highPass(sampleRate, LOW_HZ, 1.3065630)
    private val lowPass = if (sampleRate > 2 * HIGH_HZ) Biquad.lowPass(sampleRate, HIGH_HZ) else null

    /** RMS of the speech band of [frame] in dBFS (digital silence = -inf). */
    fun bandDbfs(frame: ShortArray, count: Int = frame.size): Float {
        if (count <= 0) return Float.NEGATIVE_INFINITY
        var sum = 0.0
        var allZero = true
        for (i in 0 until count) {
            val s = frame[i]
            if (s.toInt() != 0) allZero = false
            var y = highPass2.step(highPass1.step(s / 32768.0))
            if (lowPass != null) y = lowPass.step(y)
            sum += y * y
        }
        if (allZero) return Float.NEGATIVE_INFINITY      // OS-muted mic stays "digital silence"
        val rms = sqrt(sum / count)
        return if (rms <= 0.0) Float.NEGATIVE_INFINITY else (20.0 * log10(rms)).toFloat()
    }

    fun reset() { highPass1.reset(); highPass2.reset(); lowPass?.reset() }

    companion object {
        const val LOW_HZ = 80.0
        const val HIGH_HZ = 8_000.0
    }

    private class Biquad(private val b0: Double, private val b1: Double, private val b2: Double,
                         private val a1: Double, private val a2: Double) {
        private var x1 = 0.0; private var x2 = 0.0; private var y1 = 0.0; private var y2 = 0.0

        fun step(x: Double): Double {
            val y = b0 * x + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2
            x2 = x1; x1 = x; y2 = y1; y1 = y
            return y
        }

        fun reset() { x1 = 0.0; x2 = 0.0; y1 = 0.0; y2 = 0.0 }

        companion object {
            private const val Q = 0.7071067811865476     // 2nd-order Butterworth

            fun highPass(rate: Int, hz: Double, q: Double = Q): Biquad {
                val w = 2 * PI * hz / rate; val alpha = sin(w) / (2 * q); val c = cos(w); val a0 = 1 + alpha
                return Biquad((1 + c) / 2 / a0, -(1 + c) / a0, (1 + c) / 2 / a0, -2 * c / a0, (1 - alpha) / a0)
            }

            fun lowPass(rate: Int, hz: Double): Biquad {
                val w = 2 * PI * hz / rate; val alpha = sin(w) / (2 * Q); val c = cos(w); val a0 = 1 + alpha
                return Biquad((1 - c) / 2 / a0, (1 - c) / a0, (1 - c) / 2 / a0, -2 * c / a0, (1 - alpha) / a0)
            }
        }
    }
}
