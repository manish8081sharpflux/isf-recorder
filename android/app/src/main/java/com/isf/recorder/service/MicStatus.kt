package com.isf.recorder.service

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.media.AudioFormat
import android.media.AudioManager
import android.media.AudioRecord
import android.media.MediaRecorder
import androidx.core.content.ContextCompat

enum class MicReadiness(val label: String) {
    READY("Microphone ready"),
    NO_PERMISSION("Microphone permission needed"),
    IN_CALL("Microphone busy (call in progress)"),
    UNAVAILABLE("Microphone not available")
}

/** One-shot check used by Home before recording starts. Never call while a session is recording. */
object MicStatus {
    fun check(context: Context): MicReadiness {
        if (ContextCompat.checkSelfPermission(context, Manifest.permission.RECORD_AUDIO)
            != PackageManager.PERMISSION_GRANTED
        ) return MicReadiness.NO_PERMISSION

        val am = context.getSystemService(Context.AUDIO_SERVICE) as AudioManager
        if (MicConflictManager.isCallMode(am.mode)) return MicReadiness.IN_CALL

        // Our own session holds the mic: probing would create a second AudioRecord.
        if (AudioRecordingService.getActiveRecordingSessionId() != null) return MicReadiness.READY

        val minBuf = AudioRecord.getMinBufferSize(16000, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT)
        if (minBuf <= 0) return MicReadiness.UNAVAILABLE
        return try {
            @Suppress("MissingPermission")
            val rec = AudioRecord(MediaRecorder.AudioSource.MIC, 16000, AudioFormat.CHANNEL_IN_MONO,
                AudioFormat.ENCODING_PCM_16BIT, minBuf)
            val ok = rec.state == AudioRecord.STATE_INITIALIZED
            rec.release()
            if (ok) MicReadiness.READY else MicReadiness.UNAVAILABLE
        } catch (e: Exception) {
            MicReadiness.UNAVAILABLE
        }
    }
}
