package com.isf.recorder.service

import android.content.Context
import android.media.AudioDeviceCallback
import android.media.AudioDeviceInfo
import android.media.AudioManager
import android.util.Log
import com.isf.recorder.data.model.MicType
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow

/**
 * Knows which input devices exist. Preference: USB → wired headset → built-in mic.
 * Bluetooth is not used for capture (SCO is never started), so it is not reported as the mic;
 * A2DP is output-only and never a mic. The service reports the actually routed device.
 */
class MicrophoneRouter(context: Context) {
    private val audioManager = context.getSystemService(Context.AUDIO_SERVICE) as AudioManager

    private val _currentMic = MutableStateFlow(MicType.BUILT_IN)
    /** Mic that a new capture would use (USB / wired / built-in). */
    val currentMic: StateFlow<MicType> = _currentMic.asStateFlow()

    /** Called on the main thread when an external input appears ([added]) or disappears ([removed]). */
    var onExternalMicChanged: ((added: Boolean, removed: Boolean) -> Unit)? = null

    private var audioDeviceCallback: AudioDeviceCallback? = null

    companion object {
        fun isExternalType(type: Int): Boolean =
            type == AudioDeviceInfo.TYPE_WIRED_HEADSET ||
                type == AudioDeviceInfo.TYPE_USB_DEVICE ||
                type == AudioDeviceInfo.TYPE_USB_HEADSET

        fun micTypeOf(type: Int): MicType = when (type) {
            AudioDeviceInfo.TYPE_WIRED_HEADSET -> MicType.WIRED
            AudioDeviceInfo.TYPE_USB_DEVICE, AudioDeviceInfo.TYPE_USB_HEADSET -> MicType.USB
            AudioDeviceInfo.TYPE_BLUETOOTH_SCO -> MicType.BLUETOOTH   // only if the system really routes SCO input
            else -> MicType.BUILT_IN
        }

        fun isExternal(mic: MicType): Boolean = mic == MicType.WIRED || mic == MicType.USB
    }

    init {
        refresh()
        val cb = object : AudioDeviceCallback() {
            override fun onAudioDevicesAdded(addedDevices: Array<out AudioDeviceInfo>?) {
                refresh()
                if (addedDevices?.any { it.isSource && isExternalType(it.type) } == true) onExternalMicChanged?.invoke(true, false)
            }

            override fun onAudioDevicesRemoved(removedDevices: Array<out AudioDeviceInfo>?) {
                refresh()
                if (removedDevices?.any { it.isSource && isExternalType(it.type) } == true) onExternalMicChanged?.invoke(false, true)
            }
        }
        audioManager.registerAudioDeviceCallback(cb, null)   // null handler = main looper
        audioDeviceCallback = cb
    }

    private fun inputs(): Array<AudioDeviceInfo> = try {
        audioManager.getDevices(AudioManager.GET_DEVICES_INPUTS)
    } catch (e: Exception) {
        emptyArray()
    }

    private fun refresh() {
        val ext = preferredExternal()
        _currentMic.value = ext?.let { micTypeOf(it.type) } ?: MicType.BUILT_IN
        Log.d("MicrophoneRouter", "Available input: ${_currentMic.value}")
    }

    fun preferredExternal(): AudioDeviceInfo? {
        val devices = inputs()
        return devices.firstOrNull { it.type == AudioDeviceInfo.TYPE_USB_DEVICE || it.type == AudioDeviceInfo.TYPE_USB_HEADSET }
            ?: devices.firstOrNull { it.type == AudioDeviceInfo.TYPE_WIRED_HEADSET }
    }

    fun builtInMic(): AudioDeviceInfo? = inputs().firstOrNull { it.type == AudioDeviceInfo.TYPE_BUILTIN_MIC }

    /** Device to set as AudioRecord.preferredDevice. */
    fun getPreferredAudioDeviceInfo(forceBuiltIn: Boolean = false): AudioDeviceInfo? =
        if (forceBuiltIn) builtInMic() else preferredExternal() ?: builtInMic()

    fun release() {
        audioDeviceCallback?.let { try { audioManager.unregisterAudioDeviceCallback(it) } catch (_: Exception) {} }
        audioDeviceCallback = null
    }
}
