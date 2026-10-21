package com.isf.recorder.data.model

enum class RecordingStatus {
    IDLE,
    RECORDING,
    PAUSED_USER,
    PAUSED_CONFLICT,  // Auto-paused because a call or another app took the mic
    FINALIZING        // Stop pressed: uploading last chunks and calling /finish
}

enum class MicType(val label: String, val apiValue: String) {
    BUILT_IN("Built-in Microphone", "built_in"),
    WIRED("Wired External Mic", "wired"),
    USB("USB-C / External Mic", "usb"),
    BLUETOOTH("Bluetooth Headset", "bluetooth")
}

data class RecordingSessionInfo(
    val sessionId: String,
    val startedAtMs: Long = System.currentTimeMillis(),
    val durationSeconds: Int = 0,          // saved voice time (what the big timer shows)
    val chunkCount: Int = 0,
    val status: RecordingStatus = RecordingStatus.RECORDING,
    val micType: MicType = MicType.BUILT_IN,
    val currentAmplitude: Float = 0f,
    val recordingId: String? = null,       // server UUID once /start succeeded
    val sessionSeconds: Int = 0,           // wall time while not user-paused
    val voiceActive: Boolean = true,       // false = "Listening — silence not saved"
    val micMessage: String? = null,        // e.g. "External mic disconnected — using phone mic"
    val micLost: Boolean = false,          // external mic unplugged / dead: paused, waiting for mic or "use phone mic"
    val mode: String? = null,              // "schedule" | "meeting" | null (legacy)
    val title: String? = null
)

/** Set by AudioRecordingService when Stop has finished uploading and /finish was called. */
data class FinishedSession(
    val sessionId: String,
    val recordingId: String?,
    val title: String?,
    val voiceSeconds: Int,
    val finishedAtMs: Long = System.currentTimeMillis(),
    val uploadPending: Boolean = false     // true = audio still on the phone; FinishSessionWorker uploads + finishes when online
)
