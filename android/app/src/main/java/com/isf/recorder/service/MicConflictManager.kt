package com.isf.recorder.service

import android.content.Context
import android.media.AudioManager
import android.media.AudioRecordingConfiguration
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.util.Log

/**
 * Detects real microphone conflicts only (bugs A + H):
 *  - CALL: audio mode is in-call / VoIP / call screening (OnModeChangedListener on API 31+, polled
 *    every second below that).
 *  - SILENCED: the OS silences our AudioRecord (another app with priority captures, API 29+).
 * No audio focus (a recorder never needs it; it paused the user's music and every notification
 * sound paused the recording) and no camera trigger (photos / face unlock do not use the mic).
 * Read errors are handled by the service's capture thread.
 * All callbacks are delivered on the main thread.
 */
class MicConflictManager(
    context: Context,
    private val listener: Listener
) {
    enum class Reason { CALL, SILENCED }

    interface Listener {
        fun onConflictStarted(reason: Reason)
        fun onConflictEnded(reason: Reason)
    }

    companion object {
        private const val TAG = "MicConflictManager"
        private const val MODE_CALL_SCREENING = 4   // AudioManager.MODE_CALL_SCREENING (API 30)
        private const val RESOLVE_DEBOUNCE_MS = 800L
        private const val POLL_MS = 1000L

        fun isCallMode(mode: Int): Boolean =
            mode == AudioManager.MODE_IN_CALL ||
                mode == AudioManager.MODE_IN_COMMUNICATION ||
                mode == MODE_CALL_SCREENING
    }

    private val audioManager = context.getSystemService(Context.AUDIO_SERVICE) as AudioManager
    private val mainExecutorContext = context
    private val handler = Handler(Looper.getMainLooper())
    private var recordingCallback: AudioManager.AudioRecordingCallback? = null
    private var modeChangedListener: Any? = null
    private var listening = false

    @Volatile var callActive = false
        private set
    @Volatile var silenced = false
        private set

    private val resolveCall = Runnable {
        if (!isCallMode(audioManager.mode) && callActive) {
            callActive = false
            Log.i(TAG, "Call ended")
            listener.onConflictEnded(Reason.CALL)
        }
    }

    private val pollMode = object : Runnable {
        override fun run() {
            onMode(audioManager.mode)
            if (listening) handler.postDelayed(this, POLL_MS)
        }
    }

    fun isCallActive(): Boolean = isCallMode(audioManager.mode)

    /** Forget the silenced flag (our AudioRecord was released/recreated; the callback re-reports it). */
    fun clearSilenced() { silenced = false }

    private fun onMode(mode: Int) {
        if (isCallMode(mode)) {
            handler.removeCallbacks(resolveCall)
            if (!callActive) {
                callActive = true
                Log.i(TAG, "Call / VoIP started (mode=$mode)")
                listener.onConflictStarted(Reason.CALL)
            }
        } else if (callActive) {
            handler.removeCallbacks(resolveCall)
            handler.postDelayed(resolveCall, RESOLVE_DEBOUNCE_MS)
        }
    }

    fun startListening() {
        if (listening) return
        listening = true
        callActive = false
        silenced = false

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            val cb = object : AudioManager.AudioRecordingCallback() {
                override fun onRecordingConfigChanged(configs: List<AudioRecordingConfiguration>) {
                    // Only this app's recordings are reported. Empty = we released our AudioRecord: no information.
                    if (configs.isEmpty()) return
                    val nowSilenced = configs.any { it.isClientSilenced }
                    if (nowSilenced && !silenced) {
                        silenced = true
                        Log.i(TAG, "Our recording is silenced by the OS")
                        listener.onConflictStarted(Reason.SILENCED)
                    } else if (!nowSilenced && silenced) {
                        silenced = false
                        Log.i(TAG, "Our recording is no longer silenced")
                        listener.onConflictEnded(Reason.SILENCED)
                    }
                }
            }
            try {
                audioManager.registerAudioRecordingCallback(cb, handler)
                recordingCallback = cb
            } catch (e: Exception) {
                Log.w(TAG, "registerAudioRecordingCallback failed: ${e.message}")
            }
        }

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            try {
                val l = AudioManager.OnModeChangedListener { mode -> handler.post { onMode(mode) } }
                audioManager.addOnModeChangedListener(mainExecutorContext.mainExecutor, l)
                modeChangedListener = l
            } catch (e: Exception) {
                Log.w(TAG, "addOnModeChangedListener failed, polling instead: ${e.message}")
                handler.postDelayed(pollMode, POLL_MS)
            }
        } else {
            handler.postDelayed(pollMode, POLL_MS)
        }

        // A call may already be in progress.
        handler.post { onMode(audioManager.mode) }
    }

    fun stopListening() {
        listening = false
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            recordingCallback?.let { try { audioManager.unregisterAudioRecordingCallback(it) } catch (_: Exception) {} }
            recordingCallback = null
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            (modeChangedListener as? AudioManager.OnModeChangedListener)?.let {
                try { audioManager.removeOnModeChangedListener(it) } catch (_: Exception) {}
            }
            modeChangedListener = null
        }
        handler.removeCallbacksAndMessages(null)
        callActive = false
        silenced = false
    }
}
