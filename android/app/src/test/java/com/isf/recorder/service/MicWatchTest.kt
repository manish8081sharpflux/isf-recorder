package com.isf.recorder.service

import com.isf.recorder.data.model.MicType
import com.isf.recorder.service.MicWatch.Action
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Simulates the service: every fallback / re-plug reroutes, and the service feeds the actually
 * routed device back through onRouted (as updateRoutedMic does).
 */
class MicWatchTest {

    private class Sim(start: MicType) {
        val w = MicWatch()
        var alerts = 0
        init { w.onRouted(start) }

        fun unplug() { if (w.onExternalRemoved(anyExternalLeft = false)) fallback() }
        fun plug(type: MicType = MicType.USB) { if (w.onExternalAdded()) w.onRouted(type) }
        fun fallback() { if (w.markFallback()) { alerts++; w.onRouted(MicType.BUILT_IN) } }

        /** Feeds n frames; performs fallbacks; returns the non-NONE actions seen. */
        fun frames(n: Int, zero: Boolean, recording: Boolean = true): List<Action> {
            val seen = mutableListOf<Action>()
            repeat(n) {
                val a = w.onFrame(zero, recording)
                if (a != Action.NONE) seen += a
                if (a == Action.FALLBACK_TO_BUILT_IN) fallback()
            }
            return seen
        }
    }

    private val fiveSec = VoiceGate.framesFor(5_000)
    private val tenSec = VoiceGate.framesFor(10_000)

    @Test
    fun plugUnplugPlugUnplugGivesTwoAlerts() {
        val sim = Sim(MicType.USB)
        sim.unplug()
        assertEquals(1, sim.alerts)
        assertEquals(MicType.BUILT_IN, sim.w.micType)
        assertTrue(sim.w.forceBuiltIn)

        sim.plug()
        assertFalse(sim.w.forceBuiltIn)
        assertEquals(MicType.USB, sim.w.micType)

        sim.unplug()
        assertEquals(2, sim.alerts)          // the second disconnect is detected too
        assertEquals(MicType.BUILT_IN, sim.w.micType)
    }

    @Test
    fun unplugWhileOnPhoneMicOrWithAnotherExternalLeftDoesNothing() {
        val w = MicWatch().apply { onRouted(MicType.BUILT_IN) }
        assertFalse(w.onExternalRemoved(anyExternalLeft = false))
        w.onRouted(MicType.USB)
        assertFalse(w.onExternalRemoved(anyExternalLeft = true))
    }

    @Test
    fun zerosAfterReplugFallBackAgain() {
        val sim = Sim(MicType.USB)
        assertEquals(listOf(Action.FALLBACK_TO_BUILT_IN), sim.frames(fiveSec, zero = true))
        assertEquals(1, sim.alerts)
        sim.frames(10, zero = false)
        sim.plug()
        assertEquals(MicType.USB, sim.w.micType)
        assertEquals(listOf(Action.FALLBACK_TO_BUILT_IN), sim.frames(fiveSec, zero = true))
        assertEquals(2, sim.alerts)
        assertEquals(MicType.BUILT_IN, sim.w.micType)
    }

    @Test
    fun noSoundAlertOncePerEpisodeOnAnyMic() {
        val sim = Sim(MicType.BUILT_IN)
        val first = sim.frames(tenSec * 3, zero = true)
        assertEquals(listOf(Action.ALERT_NO_SOUND), first)        // once, not every 10 s
        assertEquals(listOf(Action.SOUND_RETURNED), sim.frames(1, zero = false))
        assertEquals(listOf(Action.ALERT_NO_SOUND), sim.frames(tenSec, zero = true))  // new episode
    }

    @Test
    fun noAlertBeforeTenSecondsOrWhileNotRecording() {
        val sim = Sim(MicType.BUILT_IN)
        assertTrue(sim.frames(tenSec - 1, zero = true).isEmpty())
        assertTrue(sim.frames(5, zero = false).isEmpty())          // run broken, no alert was due
        assertTrue(sim.frames(tenSec * 2, zero = true, recording = false).isEmpty())
        assertTrue(sim.frames(tenSec - 1, zero = true).isEmpty())  // counting restarted
    }

    @Test
    fun externalZerosFallBackThenAlertIfPhoneMicAlsoSilent() {
        val sim = Sim(MicType.WIRED)
        val actions = sim.frames(fiveSec + tenSec, zero = true)
        assertEquals(listOf(Action.FALLBACK_TO_BUILT_IN, Action.ALERT_NO_SOUND), actions)
    }

    @Test
    fun replugWhileOnExternalAlreadyDoesNotReroute() {
        val w = MicWatch().apply { onRouted(MicType.USB) }
        assertFalse(w.onExternalAdded())
    }
}
