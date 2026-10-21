package com.isf.recorder.service

import com.isf.recorder.data.model.MicType

/**
 * Pure mic-health decisions for one session (unit-tested; no Android types).
 *
 *  - Route: [onRouted] is fed the ACTUALLY routed device after every (re)route, so [micType] is
 *    always true (fixes the second-unplug bug: after a re-plug the mic is external again, so the
 *    next unplug / dead-mic check fires again). Every reroute re-arms zero detection.
 *  - Unplug: [onExternalRemoved] → fall back to the phone mic, every time it happens.
 *  - Re-plug: [onExternalAdded] clears the forced phone mic so the external mic is preferred again.
 *  - Zero audio (any mic): [onFrame] counts consecutive digital-zero frames while RECORDING.
 *    External mic → fallback after 5 s. Any mic → one "no sound" alert per episode after 10 s;
 *    the episode ends when sound returns ([Action.SOUND_RETURNED]).
 *
 * Thread-safe: frames come from the capture thread, route events from the main thread.
 */
class MicWatch(
    private val externalDeadFrames: Int = VoiceGate.framesFor(5_000),
    private val noSoundFrames: Int = VoiceGate.framesFor(10_000)
) {
    enum class Action { NONE, FALLBACK_TO_BUILT_IN, ALERT_NO_SOUND, SOUND_RETURNED }

    companion object {
        const val MSG_NO_SOUND = "Microphone is giving no sound — check the mic"
        fun isExternal(mic: MicType) = mic == MicType.WIRED || mic == MicType.USB
    }

    var micType: MicType = MicType.BUILT_IN
        @Synchronized get
        private set

    /** True after an external mic failed/was removed: new captures use the phone mic. */
    var forceBuiltIn = false
        @Synchronized get
        private set

    private var zeroRun = 0
    private var noSoundAlerted = false

    /** After every (re)route with the device the system really uses. Re-arms zero detection. */
    @Synchronized
    fun onRouted(type: MicType) {
        micType = type
        zeroRun = 0
    }

    /**
     * An external input disappeared. @return true when the session must fall back to the phone mic
     * (and alert) — i.e. we were on an external mic and no other external input is left.
     */
    @Synchronized
    fun onExternalRemoved(anyExternalLeft: Boolean): Boolean {
        if (!isExternal(micType) || anyExternalLeft) return false
        forceBuiltIn = true
        return true
    }

    /** An external input appeared. @return true when capture should re-route to it. */
    @Synchronized
    fun onExternalAdded(): Boolean {
        val wasForced = forceBuiltIn
        forceBuiltIn = false
        return wasForced || !isExternal(micType)
    }

    /** Capture failed / external mic dead: mark the phone mic as forced. @return false if already on it. */
    @Synchronized
    fun markFallback(): Boolean {
        if (forceBuiltIn && !isExternal(micType)) return false
        forceBuiltIn = true
        return true
    }

    /** One 512-sample frame. [recording] = RECORDING (not soft-paused / paused). */
    @Synchronized
    fun onFrame(allZero: Boolean, recording: Boolean): Action {
        if (!recording) {
            zeroRun = 0
            return Action.NONE
        }
        if (!allZero) {
            zeroRun = 0
            if (noSoundAlerted) {
                noSoundAlerted = false
                return Action.SOUND_RETURNED
            }
            return Action.NONE
        }
        zeroRun++
        if (isExternal(micType) && zeroRun == externalDeadFrames) return Action.FALLBACK_TO_BUILT_IN
        if (zeroRun >= noSoundFrames && !noSoundAlerted) {
            noSoundAlerted = true
            return Action.ALERT_NO_SOUND
        }
        return Action.NONE
    }
}
