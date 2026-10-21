package com.isf.recorder.service

import android.content.Context
import android.media.AudioAttributes
import android.media.MediaPlayer
import android.os.PowerManager
import android.util.Log
import com.isf.recorder.R

/**
 * Spoken mic alerts ("Mic disconnected" / "Mic connected" / silence warning), played from clips built into
 * the app at ALARM volume. No text-to-speech engine is used: phones such as Xiaomi block a background app
 * from reaching another app's speech service, so the voice was silent whenever Jomi was not on screen.
 * Each clip starts with a short beep so it is noticed in a noisy room.
 */
class MicVoice(context: Context) {
    private val appContext = context.applicationContext
    private var player: MediaPlayer? = null

    enum class Clip(val res: Int) {
        DISCONNECTED(R.raw.mic_disconnected),
        NOT_WORKING(R.raw.mic_not_working),
        CONNECTED(R.raw.mic_connected),
        SILENCE_STOP_WARNING(R.raw.silence_stop_warning)
    }

    fun say(clip: Clip) {
        try {
            stop()
            val afd = appContext.resources.openRawResourceFd(clip.res) ?: return
            player = MediaPlayer().apply {
                setAudioAttributes(
                    AudioAttributes.Builder()
                        .setUsage(AudioAttributes.USAGE_ALARM)
                        .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                        .build()
                )
                setWakeMode(appContext, PowerManager.PARTIAL_WAKE_LOCK)
                setDataSource(afd.fileDescriptor, afd.startOffset, afd.length)
                afd.close()
                setOnCompletionListener { it.release(); if (player === it) player = null }
                setOnErrorListener { mp, what, extra ->
                    Log.w(TAG, "Clip playback error $what/$extra"); mp.release(); if (player === mp) player = null; true
                }
                prepare()
                start()
            }
        } catch (e: Exception) {
            Log.w(TAG, "Could not play ${clip.name}: ${e.message}")
        }
    }

    private fun stop() {
        player?.let { try { it.stop() } catch (_: Exception) {}; it.release() }
        player = null
    }

    fun shutdown() = stop()

    companion object {
        private const val TAG = "MicVoice"
    }
}
