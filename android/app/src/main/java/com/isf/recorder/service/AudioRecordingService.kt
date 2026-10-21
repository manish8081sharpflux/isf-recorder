package com.isf.recorder.service

import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.AudioRouting
import android.media.MediaRecorder
import android.os.Binder
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.os.PowerManager
import android.os.Process
import android.util.Log
import com.isf.recorder.data.api.ISFApiClient
import com.isf.recorder.data.db.AppDatabase
import com.isf.recorder.data.model.FinishedSession
import com.isf.recorder.data.model.MicType
import com.isf.recorder.data.model.RecordingSessionEntity
import com.isf.recorder.data.model.RecordingSessionInfo
import com.isf.recorder.data.model.RecordingStatus
import com.isf.recorder.data.model.SessionState
import com.isf.recorder.data.model.sessionDisplayTitle
import com.isf.recorder.sync.ChunkUploader
import com.isf.recorder.sync.ReportStatusWorker
import com.isf.recorder.sync.SessionFinisher
import com.isf.recorder.sync.SessionStarter
import kotlinx.coroutines.*
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import java.util.UUID
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.atomic.AtomicLong
import kotlin.math.pow

/**
 * Foreground microphone service. Threading:
 *  - main thread: all control (start/pause/resume/stop, conflicts, mic switches) and [session] ownership;
 *  - "ISF-AudioCaptureThread": reads 512-sample frames, runs the loudness [VoiceGate], writes voice;
 *  - chunk finalizer thread (in [AudioChunkManager]): header, SHA-256, Room insert;
 *  - [serviceScope]: timer (1 s), heartbeat (60 s), Stop finalization, mic retries.
 * [_sessionState] is only changed with update {} (bug D).
 */
class AudioRecordingService : Service() {

    enum class PauseReason { USER, CALL, SILENCED, MIC_ERROR, MIC_LOST }

    /** Per-session data. Fields written by the main thread are volatile because the capture/timer threads read them. */
    private class Session(
        val id: String,
        val startedAtMs: Long,
        val mode: String?,
        val title: String?,
        val chunkManager: AudioChunkManager
    ) {
        @Volatile var recordingId: String? = null
        @Volatile var status: RecordingStatus = RecordingStatus.RECORDING
        @Volatile var pauseReason: PauseReason? = null
        @Volatile var voiceActive = false
        @Volatile var resetRequested = false
        val micWatch = MicWatch()                   // route + zero-audio decisions
        @Volatile var micType: MicType = MicType.BUILT_IN
        @Volatile var micMessage: String? = null
        @Volatile var silenceStopMs = 0L             // admin setting per mode: auto-stop after this long without voice (0 = off)
        @Volatile var lastVoiceAtMs = System.currentTimeMillis()
        @Volatile var silenceWarned = false
        @Volatile var awaitingMicChoice = false     // external mic lost: never continue on the phone mic silently
        @Volatile var micLostDead = false           // lost because it gave no sound (vs unplugged)
        val gate = VoiceGate<ShortArray>()          // capture thread only
        val silenceGaps = SilenceGapTracker()       // capture thread only
        val framesHeard = AtomicLong(0)             // frames given to the gate (not paused)
        val sessionSeconds = AtomicInteger(0)
        var remindersSent = 0
        var lastPersistMs = 0L
        var lastWakeRenewMs = 0L
    }

    private val binder = LocalBinder()
    private val serviceScope = CoroutineScope(Dispatchers.Default + SupervisorJob())
    private val mainHandler = Handler(Looper.getMainLooper())

    private var wakeLock: PowerManager.WakeLock? = null
    private var audioRecord: AudioRecord? = null            // main thread
    private var captureThread: Thread? = null               // main thread
    @Volatile private var captureGeneration = 0
    @Volatile private var session: Session? = null

    private lateinit var microphoneRouter: MicrophoneRouter
    private lateinit var micConflictManager: MicConflictManager
    private lateinit var apiClient: ISFApiClient

    private var timerJob: Job? = null
    private var heartbeatJob: Job? = null
    private var micRetryJob: Job? = null
    private var micAlarmJob: Job? = null
    private var micVoice: MicVoice? = null

    companion object {
        private const val TAG = "AudioRecordingService"

        const val ACTION_START = "com.isf.recorder.ACTION_START"
        const val ACTION_PAUSE = "com.isf.recorder.ACTION_PAUSE"
        const val ACTION_RESUME = "com.isf.recorder.ACTION_RESUME"
        const val ACTION_STOP = "com.isf.recorder.ACTION_STOP"
        const val ACTION_USE_PHONE_MIC = "com.isf.recorder.ACTION_USE_PHONE_MIC"

        // ACTION_START extras (session metadata sent to /recordings/start)
        const val EXTRA_MODE = "mode"                           // "schedule" | "meeting"
        const val EXTRA_SILENCE_STOP_MIN = "silence_stop_min"   // admin's auto-stop time for this mode (0 = off)
        private const val SILENCE_WARN_BEFORE_MS = 60_000L
        const val EXTRA_BALGRUH = "balgruh_name"
        const val EXTRA_ASSISTANT_COACH = "assistant_coach"            // joined names (legacy field)
        const val EXTRA_ASSISTANT_COACHES_JSON = "assistant_coaches_json" // JSON array [{user_id?, name}]
        const val EXTRA_PARTICIPANTS_JSON = "participants_json" // JSON array [{user_id?, name}]
        const val EXTRA_TITLE = "title"

        const val CHANNEL_ID = RecorderNotifications.CHANNEL_RECORDING
        const val CHANNEL_ID_ALERTS = RecorderNotifications.CHANNEL_ALERTS
        const val NOTIFICATION_ID = RecorderNotifications.FOREGROUND_ID
        const val ALERT_NOTIFICATION_ID = RecorderNotifications.ALERT_ID

        const val MSG_MIC_LOST = "Mic disconnected — recording paused. Plug the mic back in, or continue with the phone mic."
        const val MSG_MIC_DEAD = "Mic is giving no sound — recording paused. Check the cable, or continue with the phone mic."
        const val MSG_USING_PHONE_MIC = "Recording with the phone mic (external mic not connected)"
        const val MSG_MIC_CONNECTED = "USB-C mic connected"
        private const val MIC_ALARM_REPEAT_MS = 30_000L
        private const val RESUME_AFTER_CONNECT_MS = 1_500L
        private const val OPTIMISTIC_ID = "starting..."
        private const val HEARTBEAT_MS = 60_000L
        private const val REMINDER_EVERY_SEC = 2 * 3600
        private const val WAKE_LOCK_TIMEOUT_MS = 10 * 60_000L
        private const val WAKE_LOCK_RENEW_MS = 5 * 60_000L
        private const val PERSIST_EVERY_MS = 15_000L
        private const val READ_ERRORS_BEFORE_FAILURE = 3

        // Global StateFlow accessible to all Compose screens immediately (even before service binds)
        private val _sessionState = MutableStateFlow<RecordingSessionInfo?>(null)
        val sessionState: StateFlow<RecordingSessionInfo?> = _sessionState.asStateFlow()

        // Last session whose Stop completed (upload + /finish) — Home shows its player and status
        private val _lastFinished = MutableStateFlow<FinishedSession?>(null)
        val lastFinished: StateFlow<FinishedSession?> = _lastFinished.asStateFlow()

        /** Live input level 0..1 (~8 updates/s) for the waveform; also mirrored in sessionState.currentAmplitude. */
        private val _amplitude = MutableStateFlow(0f)
        val amplitude: StateFlow<Float> = _amplitude.asStateFlow()

        /** Session id while the service is capturing it (null once Stop moved it to FINALIZING). */
        @Volatile private var liveSessionId: String? = null

        // Global singleton access for Compose UI binding
        var instance: AudioRecordingService? = null
            private set

        fun setOptimisticStarting() {
            _sessionState.update { current ->
                current ?: RecordingSessionInfo(
                    sessionId = OPTIMISTIC_ID,
                    status = RecordingStatus.RECORDING,
                    micType = MicType.BUILT_IN,
                    durationSeconds = 0,
                    voiceActive = false,
                    currentAmplitude = 0.05f
                )
            }
        }

        /** Clears the UI state. Ignored while a session is being recorded (the service owns it then). */
        fun resetState() {
            if (instance?.session == null) _sessionState.value = null
        }

        fun getActiveRecordingSessionId(): String? = liveSessionId

        /** True while the service is capturing [sessionId]; workers must not finish it. */
        fun isLiveSession(sessionId: String): Boolean = liveSessionId == sessionId

        /** Called by SessionStarter when /start returned the recording UUID. */
        fun onRecordingIdKnown(sessionId: String, recordingId: String?) {
            if (recordingId == null) return
            instance?.session?.takeIf { it.id == sessionId }?.recordingId = recordingId
            _sessionState.update { if (it?.sessionId == sessionId) it.copy(recordingId = recordingId) else it }
            _lastFinished.update { if (it?.sessionId == sessionId && it.recordingId == null) it.copy(recordingId = recordingId) else it }
        }

        /** Called by FinishSessionWorker when a session that Stop left pending was finished. */
        fun onFinishedByWorker(sessionId: String, recordingId: String?) {
            _lastFinished.update {
                if (it?.sessionId == sessionId) it.copy(recordingId = recordingId ?: it.recordingId, uploadPending = false) else it
            }
        }

        /** Heartbeat "state" field (contract §4.2). */
        fun heartbeatState(status: RecordingStatus, voiceActive: Boolean): String = when {
            status == RecordingStatus.RECORDING && voiceActive -> "recording"
            status == RecordingStatus.RECORDING -> "listening"
            else -> "paused"
        }
    }

    inner class LocalBinder : Binder() {
        fun getService(): AudioRecordingService = this@AudioRecordingService
    }

    override fun onBind(intent: Intent?): IBinder = binder

    // ── Lifecycle ───────────────────────────────────────────────────────────────

    override fun onCreate() {
        super.onCreate()
        instance = this
        val createdAt = System.currentTimeMillis()
        apiClient = ISFApiClient(applicationContext)
        microphoneRouter = MicrophoneRouter(applicationContext)
        microphoneRouter.onExternalMicChanged = { added, removed -> onExternalMicChanged(added, removed) }
        micVoice = MicVoice(applicationContext)
        micConflictManager = MicConflictManager(applicationContext, object : MicConflictManager.Listener {
            override fun onConflictStarted(reason: MicConflictManager.Reason) = handleConflictStarted(reason)
            override fun onConflictEnded(reason: MicConflictManager.Reason) = handleConflictEnded(reason)
        })
        // Orphans of a previous service instance (process kept alive, service destroyed).
        serviceScope.launch(Dispatchers.IO) {
            try { OrphanRecovery.run(applicationContext, cutoffMs = createdAt) } catch (t: Throwable) {
                Log.e(TAG, "Orphan recovery failed: ${t.message}", t)
            }
        }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_START -> handleStart(intent)
            ACTION_PAUSE -> userPause()
            ACTION_RESUME -> userResume()
            ACTION_STOP -> stop()
            ACTION_USE_PHONE_MIC -> usePhoneMic()
        }
        if (session == null) tearDownIfIdle()
        // Android 14 cannot restart a microphone FGS from the background; orphans are recovered instead (bug M).
        return START_NOT_STICKY
    }

    override fun onDestroy() {
        micAlarmJob?.cancel()
        micVoice?.shutdown()
        val s = session
        if (s != null) {
            Log.w(TAG, "Service destroyed with session ${s.id} in ${s.status}; saving it for the finish worker")
            if (s.status != RecordingStatus.FINALIZING) {
                s.chunkManager.suspendWrites()
                stopCapture()
                liveSessionId = null
                val voice = s.chunkManager.savedVoiceSeconds
                runBlocking(Dispatchers.IO) {
                    withTimeoutOrNull(3_000) {
                        s.chunkManager.awaitFinalized(2_500)
                        val dao = AppDatabase.getInstance(applicationContext).sessionDao()
                        dao.updateProgress(s.id, s.chunkManager.getCompletedChunkCount(), voice,
                            s.sessionSeconds.get(), silenceSkippedSeconds(s))
                        dao.markStopped(s.id)
                    }
                }
                _lastFinished.value = FinishedSession(s.id, s.recordingId, s.title, voice, uploadPending = true)
                if (s.chunkManager.getCompletedChunkCount() > 0) {
                    RecorderNotifications.notifyInterrupted(applicationContext, s.id, s.startedAtMs)
                }
            }
            SessionFinisher.enqueueFinishWorker(applicationContext, s.id)
            session = null
        }
        timerJob?.cancel(); heartbeatJob?.cancel(); micRetryJob?.cancel()
        micConflictManager.stopListening()
        microphoneRouter.release()
        releaseWakeLock()
        serviceScope.cancel()
        liveSessionId = null
        instance = null
        _sessionState.value = null
        _amplitude.value = 0f
        super.onDestroy()
    }

    // ── Start ───────────────────────────────────────────────────────────────────

    private fun handleStart(intent: Intent) {
        if (session != null) {
            Log.w(TAG, "ACTION_START ignored: session ${session?.id} exists (${session?.status})")
            return
        }
        fun extra(key: String) = intent.getStringExtra(key)?.trim()?.takeIf { it.isNotEmpty() }
        val mode = extra(EXTRA_MODE)
        val balgruh = extra(EXTRA_BALGRUH)
        val coach = extra(EXTRA_ASSISTANT_COACH)
        val coachesJson = extra(EXTRA_ASSISTANT_COACHES_JSON)
        val participants = extra(EXTRA_PARTICIPANTS_JSON)
        val rawTitle = extra(EXTRA_TITLE)
        val sid = "rec_" + UUID.randomUUID().toString()
        val startedAt = System.currentTimeMillis()
        val displayTitle = sessionDisplayTitle(mode, balgruh, rawTitle, coach)

        // Foreground first: startForegroundService() requires it within seconds.
        try {
            val content = RecorderNotifications.foregroundContent(RecordingStatus.RECORDING, false, 0, false, null)
            val n = RecorderNotifications.buildForeground(this, content)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                startForeground(NOTIFICATION_ID, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE)
            } else {
                startForeground(NOTIFICATION_ID, n)
            }
        } catch (t: Throwable) {
            Log.e(TAG, "startForeground failed (permission?): ${t.message}", t)
            _sessionState.update { if (it?.sessionId == OPTIMISTIC_ID) null else it }
            stopSelf()
            return
        }
        acquireWakeLock()

        val micType = microphoneRouter.currentMic.value
        // Persist before anything can be uploaded, so every chunk is sent after /start with metadata.
        try {
            runBlocking(Dispatchers.IO) {
                AppDatabase.getInstance(applicationContext).sessionDao().insert(
                    RecordingSessionEntity(
                        sessionId = sid, state = SessionState.ACTIVE, startedAtMs = startedAt,
                        micSource = micType.apiValue, mode = mode, balgruhName = balgruh,
                        assistantCoach = coach, participantsJson = participants, title = rawTitle,
                        assistantCoachesJson = coachesJson
                    )
                )
            }
        } catch (t: Throwable) {
            Log.e(TAG, "Could not persist session $sid: ${t.message}", t)
        }

        val chunkManager = AudioChunkManager(applicationContext, sid) { chunk ->
            ChunkUploader.uploadInBackground(applicationContext, chunk)
        }
        val s = Session(sid, startedAt, mode, displayTitle, chunkManager).apply {
            this.micType = micType
            silenceStopMs = intent.getIntExtra(EXTRA_SILENCE_STOP_MIN, 0).coerceIn(0, 180) * 60_000L
        }
        session = s
        liveSessionId = sid
        _sessionState.value = RecordingSessionInfo(
            sessionId = sid,
            startedAtMs = startedAt,
            durationSeconds = 0,
            sessionSeconds = 0,
            chunkCount = 0,
            status = RecordingStatus.RECORDING,
            voiceActive = false,
            micType = micType,
            mode = mode,
            title = displayTitle
        )

        serviceScope.launch(Dispatchers.IO) { SessionStarter.ensureStarted(applicationContext, sid) }

        micConflictManager.startListening()
        if (!startCapture()) enterConflict(PauseReason.MIC_ERROR)
        startTimerLoop()
        startHeartbeatLoop()
        Log.i(TAG, "Session $sid started (mode=$mode, mic=$micType)")
    }

    // ── Capture ─────────────────────────────────────────────────────────────────

    /** Main thread. Stops any previous capture (joined), then opens a new AudioRecord + thread. */
    private fun startCapture(): Boolean {
        val s = session ?: return false
        stopCapture()
        val minBuf = AudioRecord.getMinBufferSize(
            AudioChunkManager.SAMPLE_RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT
        )
        if (minBuf <= 0) {
            Log.e(TAG, "getMinBufferSize failed: $minBuf")
            return false
        }
        // ≥ 2 s of audio (bug G): absorbs model load, GC pauses and slow storage without overruns.
        val bufferSize = maxOf(minBuf, AudioChunkManager.BYTES_PER_SECOND * 2)

        fun create(): AudioRecord? = try {
            @Suppress("MissingPermission")
            AudioRecord(MediaRecorder.AudioSource.MIC, AudioChunkManager.SAMPLE_RATE,
                AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT, bufferSize)
                .takeIf { it.state == AudioRecord.STATE_INITIALIZED || run { it.release(); false } }
        } catch (t: Throwable) {
            Log.w(TAG, "AudioRecord creation failed: ${t.message}")
            null
        }

        var record = create()
        if (record == null) {
            Thread.sleep(100)   // the HAL may still be releasing the previous instance
            record = create()
        }
        if (record == null) return false

        try {
            microphoneRouter.getPreferredAudioDeviceInfo(s.micWatch.forceBuiltIn)?.let { record.preferredDevice = it }
            record.startRecording()
        } catch (t: Throwable) {
            Log.e(TAG, "startRecording failed: ${t.message}")
        }
        if (record.recordingState != AudioRecord.RECORDSTATE_RECORDING) {
            try { record.release() } catch (_: Throwable) {}
            return false
        }
        audioRecord = record
        updateRoutedMic(record)
        try {
            record.addOnRoutingChangedListener(AudioRouting.OnRoutingChangedListener { updateRoutedMic(record) }, mainHandler)
        } catch (_: Throwable) {}

        val gen = ++captureGeneration
        captureThread = Thread({ captureLoop(record, gen, s) }, "ISF-AudioCaptureThread").apply { start() }
        return true
    }

    /** Main thread. Ends the capture thread (always joined, bug F) and releases the AudioRecord. */
    private fun stopCapture() {
        captureGeneration++
        val record = audioRecord
        audioRecord = null
        try { record?.stop() } catch (_: Throwable) {}
        val t = captureThread
        captureThread = null
        if (t != null && t !== Thread.currentThread()) {
            try { t.join(2_000) } catch (_: InterruptedException) {}
            if (t.isAlive) Log.e(TAG, "Capture thread did not exit within 2 s")
        }
        try {
            record?.preferredDevice = null
            record?.release()
        } catch (_: Throwable) {}
        _amplitude.value = 0f
    }

    private fun updateRoutedMic(record: AudioRecord) {
        val s = session ?: return
        val routed = try { record.routedDevice } catch (_: Throwable) { null }
        val type = routed?.let { MicrophoneRouter.micTypeOf(it.type) }
            ?: if (s.micWatch.forceBuiltIn) MicType.BUILT_IN else microphoneRouter.currentMic.value
        if (type != s.micType) Log.i(TAG, "Routed input: $type")
        s.micType = type
        s.micWatch.onRouted(type)
        _sessionState.update { if (it?.sessionId == s.id) it.copy(micType = type) else it }
    }

    /** Capture thread body: one AudioRecord, one generation. Never touches the AudioRecord lifecycle. */
    private fun captureLoop(record: AudioRecord, gen: Int, s: Session) {
        try { Process.setThreadPriority(Process.THREAD_PRIORITY_URGENT_AUDIO) } catch (_: Throwable) {}
        val frame = ShortArray(VoiceGate.FRAME_SAMPLES)
        var filled = 0
        var errorStreak = 0
        var ampFrames = 0

        while (captureGeneration == gen) {
            val n = try { record.read(frame, filled, frame.size - filled) } catch (t: Throwable) { -1 }
            if (captureGeneration != gen) break
            if (n < 0) {
                errorStreak++
                Log.w(TAG, "AudioRecord read error $n (streak $errorStreak)")
                if (errorStreak >= READ_ERRORS_BEFORE_FAILURE) {
                    mainHandler.post { onCaptureFailed(gen, "read error $n") }
                    break
                }
                try { Thread.sleep(50) } catch (_: InterruptedException) { break }
                continue
            }
            if (n == 0) {
                if (record.recordingState != AudioRecord.RECORDSTATE_RECORDING) {
                    mainHandler.post { onCaptureFailed(gen, "recording stopped by the system") }
                    break
                }
                try { Thread.sleep(5) } catch (_: InterruptedException) { break }
                continue
            }
            errorStreak = 0
            filled += n
            if (filled < frame.size) continue
            filled = 0

            val dbfs = VoiceGate.rmsDbfs(frame)
            if (++ampFrames >= 4) {        // ~8 updates per second
                ampFrames = 0
                val amp = if (dbfs.isInfinite()) 0f else 10.0.pow(dbfs / 20.0).toFloat().coerceIn(0f, 1f)
                _amplitude.value = amp
                _sessionState.update { if (it?.sessionId == s.id) it.copy(currentAmplitude = amp) else it }
            }

            // Digital zeros: external mic → phone mic after 5 s; any mic → "no sound" alert after 10 s.
            // Not while soft-paused: a silenced client receives zeros by design.
            when (s.micWatch.onFrame(dbfs.isInfinite(), s.status == RecordingStatus.RECORDING)) {
                MicWatch.Action.FALLBACK_TO_BUILT_IN -> mainHandler.post { onExternalMicDead(gen) }
                MicWatch.Action.ALERT_NO_SOUND -> mainHandler.post { onNoSound(true) }
                MicWatch.Action.SOUND_RETURNED -> mainHandler.post { onNoSound(false) }
                MicWatch.Action.NONE -> Unit
            }

            if (s.status != RecordingStatus.RECORDING) continue   // soft pause: keep the mic, save nothing
            if (s.resetRequested) {
                s.resetRequested = false
                s.gate.reset()
                s.silenceGaps.breakRun()?.let { persistGaps(s) }
            }
            s.framesHeard.incrementAndGet()
            val d = s.gate.onFrame(frame.copyOf(), dbfs)
            s.silenceGaps.onFrame(d.write.size, System.currentTimeMillis())?.let { persistGaps(s) }
            if (d.write.isNotEmpty()) s.chunkManager.writeFrames(d.write)
            if (d.closeChunk) s.chunkManager.closeCurrentChunk()
            if (d.voiceActive) s.lastVoiceAtMs = System.currentTimeMillis()
            if (d.voiceChanged) {
                s.voiceActive = d.voiceActive
                _sessionState.update { if (it?.sessionId == s.id) it.copy(voiceActive = d.voiceActive) else it }
                refreshNotification()
            }
        }
    }

    /** Saved with the Room session so a later FinishSessionWorker sends the markers too. */
    private fun persistGaps(s: Session) {
        val json = SilenceGapTracker.toJson(s.silenceGaps.gaps)
        serviceScope.launch(Dispatchers.IO) {
            try { AppDatabase.getInstance(applicationContext).sessionDao().updateSilenceGaps(s.id, json) }
            catch (e: Exception) { Log.w(TAG, "Could not save silence markers: ${e.message}") }
        }
    }

    // ── Conflicts and failures (main thread) ────────────────────────────────────

    private fun handleConflictStarted(reason: MicConflictManager.Reason) {
        when (reason) {
            MicConflictManager.Reason.CALL -> enterConflict(PauseReason.CALL)
            MicConflictManager.Reason.SILENCED -> enterConflict(PauseReason.SILENCED)
        }
    }

    private fun handleConflictEnded(reason: MicConflictManager.Reason) {
        val s = session ?: return
        if (s.status != RecordingStatus.PAUSED_CONFLICT) return
        if (s.pauseReason == PauseReason.MIC_LOST) return
        if (micConflictManager.callActive || micConflictManager.isCallActive()) return
        if (reason == MicConflictManager.Reason.CALL) micConflictManager.clearSilenced()
        else if (micConflictManager.silenced) return
        Log.i(TAG, "Conflict ($reason) ended; auto-resuming")
        resumeInternal(fromUser = false)
    }

    /**
     * RECORDING → PAUSED_CONFLICT (or a stronger reason while already conflict-paused).
     * CALL / MIC_ERROR release the microphone; SILENCED keeps it (the OS feeds zeros) so the
     * un-silence callback can resume us without flapping.
     */
    private fun enterConflict(reason: PauseReason) {
        val s = session ?: return
        val wasRecording = s.status == RecordingStatus.RECORDING
        if (!wasRecording && s.status != RecordingStatus.PAUSED_CONFLICT) return   // user pause / finalizing win
        if (!wasRecording && s.pauseReason == PauseReason.CALL && reason != PauseReason.CALL) return  // keep the stronger reason
        val alreadyMicLost = s.status == RecordingStatus.PAUSED_CONFLICT && s.pauseReason == PauseReason.MIC_LOST
        s.chunkManager.suspendWrites()
        if (reason != PauseReason.SILENCED) stopCapture()
        s.status = RecordingStatus.PAUSED_CONFLICT
        s.pauseReason = reason
        s.voiceActive = false
        s.micMessage = when (reason) {
            PauseReason.CALL -> "Paused — a call is using the microphone"
            PauseReason.SILENCED -> "Paused — another app is using the microphone"
            PauseReason.MIC_LOST -> if (s.micLostDead) MSG_MIC_DEAD else MSG_MIC_LOST
            else -> "Microphone unavailable — retrying…"
        }
        _sessionState.update {
            if (it?.sessionId == s.id) it.copy(status = RecordingStatus.PAUSED_CONFLICT, voiceActive = false,
                currentAmplitude = 0f, micMessage = s.micMessage, micLost = reason == PauseReason.MIC_LOST) else it
        }
        refreshNotification()
        if (reason == PauseReason.MIC_LOST) {
            // Instruction 3: pause, say it out loud, repeat every 30 s until the mic is back or the coach decides.
            if (!alreadyMicLost) {
                RecorderNotifications.alert(this, "🎙️ ${if (s.micLostDead) "Mic not working" else "Mic disconnected"} — recording paused",
                    "Plug the mic back in to continue automatically, or open the app and choose \"Continue with phone mic\".")
                val sid = s.id
                serviceScope.launch(Dispatchers.IO) { apiClient.pauseRecording(sid, "mic_disconnected") }
                startMicAlarm()
            }
            return
        }
        if (wasRecording) {
            RecorderNotifications.alert(
                this, "⚠️ Recording Paused",
                if (reason == PauseReason.MIC_ERROR) "The microphone stopped working. Retrying automatically — tap Resume to try now."
                else "Microphone in use by a call or another app. Recording will auto-resume when free."
            )
            triggerVibration(longArrayOf(0, 150, 100, 150))
            val sid = s.id
            val apiReason = when (reason) { PauseReason.CALL -> "call"; PauseReason.SILENCED -> "mic_silenced"; else -> "mic_error" }
            serviceScope.launch(Dispatchers.IO) { apiClient.pauseRecording(sid, apiReason) }
        }
        if (reason == PauseReason.MIC_ERROR && micRetryJob?.isActive != true) scheduleMicRetry()
    }

    /** Read errors: try the phone mic if an external one failed, else retry; never stuck forever. */
    private fun onCaptureFailed(gen: Int, why: String) {
        if (gen != captureGeneration) return
        val s = session ?: return
        if (s.status == RecordingStatus.FINALIZING || s.status == RecordingStatus.PAUSED_USER) return
        if (s.pauseReason == PauseReason.MIC_LOST) return   // already waiting for the mic / the coach
        Log.w(TAG, "Capture failed: $why")
        if (MicrophoneRouter.isExternal(s.micType) && !s.micWatch.forceBuiltIn) {
            onMicLost(dead = true)
            return
        }
        stopCapture()
        enterConflict(if (micConflictManager.isCallActive()) PauseReason.CALL else PauseReason.MIC_ERROR)
    }

    private fun scheduleMicRetry() {
        micRetryJob?.cancel()
        micRetryJob = serviceScope.launch(Dispatchers.Main) {
            var delayMs = 2_000L
            while (isActive) {
                delay(delayMs)
                delayMs = (delayMs * 2).coerceAtMost(10_000L)
                val s = session ?: break
                if (s.status != RecordingStatus.PAUSED_CONFLICT || s.pauseReason != PauseReason.MIC_ERROR) break
                if (micConflictManager.isCallActive()) continue
                if (resumeInternal(fromUser = false)) break
            }
        }
    }

    private fun onExternalMicDead(gen: Int) {
        if (gen != captureGeneration) return
        Log.w(TAG, "External mic delivers only digital zeros for 5 s")
        onMicLost(dead = true)
    }

    private fun onExternalMicChanged(added: Boolean, removed: Boolean) {
        val s = session ?: return
        if (s.status == RecordingStatus.FINALIZING) return
        if (removed) {
            if (s.micWatch.onExternalRemoved(anyExternalLeft = microphoneRouter.preferredExternal() != null)) onMicLost(dead = false)
        } else if (added) {
            val reroute = s.micWatch.onExternalAdded()
            if (s.awaitingMicChoice) {
                // Mic is back after a disconnect: resume automatically on it (user's choice, 13 Oct).
                s.awaitingMicChoice = false
                micAlarmJob?.cancel()
                micVoice?.say(MicVoice.Clip.CONNECTED)
                if (s.status == RecordingStatus.PAUSED_CONFLICT && s.pauseReason == PauseReason.MIC_LOST) {
                    // Resume after the voice prompt so "Mic connected" is not recorded.
                    mainHandler.postDelayed({
                        val cur = session
                        if (cur === s && cur.status == RecordingStatus.PAUSED_CONFLICT && cur.pauseReason == PauseReason.MIC_LOST) {
                            if (resumeInternal(fromUser = false)) flashMicConnected(cur)
                        }
                    }, RESUME_AFTER_CONNECT_MS)
                } else {
                    s.micMessage = null
                    _sessionState.update { if (it?.sessionId == s.id) it.copy(micMessage = null, micLost = false) else it }
                }
                return
            }
            if (reroute) {
                // A mic was plugged in while recording on the phone mic: switch to it, same chunk.
                // Restarting capture re-reads the routed device, so micType is external again and the
                // next unplug / dead-mic check fires.
                if (audioRecord != null) {
                    if (!startCapture()) enterConflict(PauseReason.MIC_ERROR)
                }
                triggerVibration(longArrayOf(0, 150))
                flashMicConnected(s)
            }
        }
    }

    /** Any mic gives pure digital zeros for 10 s while recording (once per episode). */
    private fun onNoSound(noSound: Boolean) {
        val s = session ?: return
        if (noSound) {
            Log.w(TAG, MicWatch.MSG_NO_SOUND)
            s.micMessage = MicWatch.MSG_NO_SOUND
            RecorderNotifications.alert(this, "🎙️ ${MicWatch.MSG_NO_SOUND}", "Recording continues, but nothing is being heard.")
            triggerVibration(longArrayOf(0, 150, 100, 150))
        } else if (s.micMessage == MicWatch.MSG_NO_SOUND) {
            s.micMessage = null
        } else return
        _sessionState.update { if (it?.sessionId == s.id) it.copy(micMessage = s.micMessage) else it }
    }

    /**
     * Instruction 3 (13 Oct): the external mic was unplugged or gives no sound. Never continue on the
     * phone mic by itself: pause, alert by voice + vibration (every 30 s), and wait until the mic is
     * plugged back in (auto-resume) or the coach taps "Continue with phone mic".
     */
    private fun onMicLost(dead: Boolean) {
        val s = session ?: return
        if (s.status == RecordingStatus.FINALIZING) return
        Log.w(TAG, if (dead) "External mic gives no sound — pausing" else "External mic disconnected — pausing")
        s.awaitingMicChoice = true
        s.micLostDead = dead
        micRetryJob?.cancel()
        when (s.status) {
            RecordingStatus.RECORDING -> enterConflict(PauseReason.MIC_LOST)
            RecordingStatus.PAUSED_CONFLICT ->
                if (s.pauseReason != PauseReason.CALL) enterConflict(PauseReason.MIC_LOST)   // after the call, resume asks again
            else -> {   // user-paused: resume will ask (see resumeInternal)
                s.micMessage = if (dead) MSG_MIC_DEAD else MSG_MIC_LOST
                _sessionState.update { if (it?.sessionId == s.id) it.copy(micMessage = s.micMessage) else it }
            }
        }
    }

    /** "Continue with phone mic" on the mic-lost prompt. */
    private fun usePhoneMic() {
        val s = session ?: return
        if (s.status == RecordingStatus.FINALIZING) return
        Log.i(TAG, "Coach chose to continue with the phone mic")
        s.micWatch.markFallback()           // new captures use the built-in mic
        s.awaitingMicChoice = false
        micAlarmJob?.cancel()
        if (s.status == RecordingStatus.PAUSED_CONFLICT || s.status == RecordingStatus.PAUSED_USER) {
            if (resumeInternal(fromUser = true)) {
                s.micMessage = MSG_USING_PHONE_MIC
                _sessionState.update { if (it?.sessionId == s.id) it.copy(micMessage = s.micMessage) else it }
            }
        }
    }

    private fun startMicAlarm() {
        micAlarmJob?.cancel()
        micAlarmJob = serviceScope.launch(Dispatchers.Main) {
            while (isActive) {
                val s = session ?: break
                if (s.status != RecordingStatus.PAUSED_CONFLICT || s.pauseReason != PauseReason.MIC_LOST) break
                micVoice?.say(if (s.micLostDead) MicVoice.Clip.NOT_WORKING else MicVoice.Clip.DISCONNECTED)
                triggerVibration(longArrayOf(0, 400, 200, 400))
                delay(MIC_ALARM_REPEAT_MS)
            }
        }
    }

    /** Instruction 1: a short, prominent "USB-C mic connected" message (cleared after a few seconds). */
    private fun flashMicConnected(s: Session) {
        s.micMessage = MSG_MIC_CONNECTED
        _sessionState.update { if (it?.sessionId == s.id) it.copy(micType = s.micType, micMessage = s.micMessage, micLost = false) else it }
        mainHandler.postDelayed({
            if (s.micMessage == MSG_MIC_CONNECTED) {
                s.micMessage = null
                _sessionState.update { if (it?.sessionId == s.id) it.copy(micMessage = null) else it }
            }
        }, 5_000)
    }

    // ── Pause / resume (bug C) ──────────────────────────────────────────────────

    private fun userPause() {
        val s = session ?: return
        if (s.status != RecordingStatus.RECORDING && s.status != RecordingStatus.PAUSED_CONFLICT) return
        val wasRecording = s.status == RecordingStatus.RECORDING
        micRetryJob?.cancel()
        s.chunkManager.suspendWrites()
        stopCapture()
        s.status = RecordingStatus.PAUSED_USER
        s.pauseReason = PauseReason.USER
        s.voiceActive = false
        s.micMessage = null
        _sessionState.update {
            if (it?.sessionId == s.id) it.copy(status = RecordingStatus.PAUSED_USER, voiceActive = false,
                currentAmplitude = 0f, micMessage = null) else it
        }
        refreshNotification()
        if (wasRecording) {
            val sid = s.id
            serviceScope.launch(Dispatchers.IO) { apiClient.pauseRecording(sid, "user") }
        }
    }

    private fun userResume() {
        val s = session ?: return
        if (s.status != RecordingStatus.PAUSED_USER && s.status != RecordingStatus.PAUSED_CONFLICT) return
        resumeInternal(fromUser = true)
    }

    /** PAUSED_* → RECORDING. Refuses during a call (then waits for the call to end). */
    private fun resumeInternal(fromUser: Boolean): Boolean {
        val s = session ?: return false
        if (s.status != RecordingStatus.PAUSED_USER && s.status != RecordingStatus.PAUSED_CONFLICT) return false
        if (micConflictManager.isCallActive()) {
            if (fromUser) {
                // The user wants to record: resume automatically when the call ends.
                if (s.status == RecordingStatus.PAUSED_USER) stopCapture()
                s.status = RecordingStatus.PAUSED_CONFLICT
                s.pauseReason = PauseReason.CALL
                s.micMessage = "Can't resume during a call — it resumes automatically when the call ends"
                _sessionState.update {
                    if (it?.sessionId == s.id) it.copy(status = RecordingStatus.PAUSED_CONFLICT, micMessage = s.micMessage) else it
                }
                refreshNotification()
            }
            return false
        }
        if (s.awaitingMicChoice && microphoneRouter.preferredExternal() == null) {
            // External mic still missing: show the mic-lost prompt instead of using the phone mic.
            if (s.status == RecordingStatus.PAUSED_USER) s.pauseReason = null
            s.status = RecordingStatus.PAUSED_CONFLICT
            if (s.pauseReason == PauseReason.CALL) s.pauseReason = null
            enterConflict(PauseReason.MIC_LOST)
            return false
        }
        if (s.awaitingMicChoice) {   // a mic is plugged in again
            s.awaitingMicChoice = false
            s.micWatch.onExternalAdded()
        }
        micAlarmJob?.cancel()
        val wasConflict = s.status == RecordingStatus.PAUSED_CONFLICT
        val previousReason = s.pauseReason
        val captureRunning = audioRecord != null && captureThread?.isAlive == true
        if (!captureRunning) {
            micConflictManager.clearSilenced()
            if (!startCapture()) {
                if (fromUser) {
                    s.status = RecordingStatus.PAUSED_CONFLICT   // let enterConflict take over
                }
                enterConflict(PauseReason.MIC_ERROR)
                return false
            }
        }
        micRetryJob?.cancel()
        s.resetRequested = true             // discontinuous audio: fresh gate + VAD state
        s.chunkManager.resumeWrites()
        s.pauseReason = null
        s.voiceActive = false
        if (s.micMessage != MSG_USING_PHONE_MIC) s.micMessage = null
        s.status = RecordingStatus.RECORDING
        _sessionState.update {
            if (it?.sessionId == s.id) it.copy(status = RecordingStatus.RECORDING, voiceActive = false, micMessage = s.micMessage, micLost = false) else it
        }
        refreshNotification()
        if (wasConflict && previousReason != PauseReason.USER) {
            RecorderNotifications.alert(this, "▶️ Recording Resumed",
                if (previousReason == PauseReason.MIC_LOST) "The microphone is back. Recording has resumed."
                else "Microphone is free again. Recording has resumed.")
            triggerVibration(longArrayOf(0, 200))
        }
        val sid = s.id
        serviceScope.launch(Dispatchers.IO) { apiClient.resumeRecording(sid) }
        return true
    }

    // ── Stop (bugs B, I) ────────────────────────────────────────────────────────

    private fun stop() {
        val s = session ?: return
        if (s.status == RecordingStatus.FINALIZING) return   // single finish
        Log.i(TAG, "Stop requested for ${s.id}")
        micRetryJob?.cancel(); micAlarmJob?.cancel(); timerJob?.cancel(); heartbeatJob?.cancel()
        s.chunkManager.suspendWrites()        // closes the last chunk → finalizer
        stopCapture()
        micConflictManager.stopListening()
        s.status = RecordingStatus.FINALIZING
        s.voiceActive = false
        liveSessionId = null
        val voice = s.chunkManager.savedVoiceSeconds
        _sessionState.update {
            if (it?.sessionId == s.id) it.copy(status = RecordingStatus.FINALIZING, voiceActive = false,
                currentAmplitude = 0f, durationSeconds = voice, sessionSeconds = s.sessionSeconds.get(),
                chunkCount = s.chunkManager.getCompletedChunkCount()) else it
        }
        refreshNotification()
        serviceScope.launch(Dispatchers.IO) { finalizeSession(s) }
    }

    private suspend fun finalizeSession(s: Session) {
        val app = applicationContext
        val dao = AppDatabase.getInstance(app).sessionDao()
        var outcome = SessionFinisher.Outcome.PENDING
        var recordingId: String? = s.recordingId
        val totalChunks = s.chunkManager.getCompletedChunkCount()
        val voice = s.chunkManager.savedVoiceSeconds
        try {
            // B: the last chunk is header-fixed and in Room before anything checks for pending chunks.
            s.chunkManager.awaitFinalized(60_000)
            s.chunkManager.shutdown()
            s.silenceGaps.breakRun()   // trailing silence before Stop
            dao.updateSilenceGaps(s.id, SilenceGapTracker.toJson(s.silenceGaps.gaps))
            dao.updateProgress(s.id, totalChunks, voice, s.sessionSeconds.get(), silenceSkippedSeconds(s))
            dao.markStopped(s.id)
            val budgetMs = (totalChunks * 2_000L).coerceIn(60_000L, 300_000L)
            val result = SessionFinisher.uploadAndFinish(app, s.id, source = "client",
                deadlineMs = System.currentTimeMillis() + budgetMs)
            outcome = result.outcome
            recordingId = result.recordingId ?: recordingId ?: dao.get(s.id)?.recordingId
        } catch (t: Throwable) {
            Log.e(TAG, "Finalizing ${s.id} failed: ${t.message}", t)
        }
        if (outcome == SessionFinisher.Outcome.PENDING) SessionFinisher.enqueueFinishWorker(app, s.id)
        if (outcome == SessionFinisher.Outcome.FINISHED && recordingId != null && totalChunks > 0) {
            ReportStatusWorker.enqueue(app, recordingId, s.title)
        }
        if (outcome != SessionFinisher.Outcome.DISCARDED) {
            _lastFinished.value = FinishedSession(
                sessionId = s.id, recordingId = recordingId, title = s.title, voiceSeconds = voice,
                uploadPending = outcome == SessionFinisher.Outcome.PENDING
            )
        }
        Log.i(TAG, "Session ${s.id} stopped: $outcome, $totalChunks chunks, ${voice}s voice")
        withContext(Dispatchers.Main) {
            if (session === s) session = null
            _sessionState.update { if (it?.sessionId == s.id) null else it }
            tearDownIfIdle()
        }
    }

    /** Heartbeat said the recording was erased on the server: stop locally without finishing. */
    private fun discardActiveSession(s: Session) {
        if (session !== s || s.status == RecordingStatus.FINALIZING) return
        Log.w(TAG, "Session ${s.id} was deleted on the server; stopping without finish")
        micRetryJob?.cancel(); micAlarmJob?.cancel(); timerJob?.cancel(); heartbeatJob?.cancel()
        s.chunkManager.suspendWrites()
        stopCapture()
        micConflictManager.stopListening()
        s.status = RecordingStatus.FINALIZING
        liveSessionId = null
        serviceScope.launch(Dispatchers.IO) {
            s.chunkManager.awaitFinalized(10_000)
            s.chunkManager.shutdown()
            SessionFinisher.discardLocal(applicationContext, s.id)
            withContext(Dispatchers.Main) {
                if (session === s) session = null
                _sessionState.update { if (it?.sessionId == s.id) null else it }
                RecorderNotifications.alert(this@AudioRecordingService, "Recording stopped", "This recording was deleted, so it stopped.")
                tearDownIfIdle()
            }
        }
    }

    private fun tearDownIfIdle() {
        if (session != null) return
        try {
            stopForeground(STOP_FOREGROUND_REMOVE)
        } catch (t: Throwable) {
            Log.w(TAG, "stopForeground: ${t.message}")
        }
        releaseWakeLock()
        stopSelf()
    }

    // ── Timer, heartbeat ────────────────────────────────────────────────────────

    private fun silenceSkippedSeconds(s: Session): Int {
        val heard = s.framesHeard.get() * VoiceGate.FRAME_SAMPLES / AudioChunkManager.SAMPLE_RATE
        return (heard - s.chunkManager.savedVoiceSeconds).toInt().coerceAtLeast(0)
    }

    private fun startTimerLoop() {
        timerJob?.cancel()
        timerJob = serviceScope.launch {
            while (isActive) {
                delay(1000)
                val s = session ?: break
                if (s.status == RecordingStatus.FINALIZING) break
                // sessionSeconds = wall time while not user-paused
                if (s.status != RecordingStatus.PAUSED_USER) s.sessionSeconds.incrementAndGet()
                val voice = s.chunkManager.savedVoiceSeconds
                val sessionSec = s.sessionSeconds.get()
                val chunks = s.chunkManager.getCompletedChunkCount()
                _sessionState.update {
                    if (it?.sessionId == s.id) it.copy(durationSeconds = voice, sessionSeconds = sessionSec, chunkCount = chunks) else it
                }
                refreshNotification()

                val now = System.currentTimeMillis()
                if (now - s.lastWakeRenewMs >= WAKE_LOCK_RENEW_MS) {
                    s.lastWakeRenewMs = now
                    renewWakeLock()
                }
                if (now - s.lastPersistMs >= PERSIST_EVERY_MS) {
                    s.lastPersistMs = now
                    try {
                        AppDatabase.getInstance(applicationContext).sessionDao()
                            .updateProgress(s.id, chunks, voice, sessionSec, silenceSkippedSeconds(s))
                    } catch (t: Throwable) { Log.w(TAG, "persist progress: ${t.message}") }
                }
                checkSilenceStop(s, now)
                checkExternalMicStillThere(s)
                val due = sessionSec / REMINDER_EVERY_SEC
                if (due > s.remindersSent) {
                    s.remindersSent = due
                    RecorderNotifications.notifyReminder(applicationContext, due * 2)
                }
            }
        }
    }

    /**
     * Admin's silence timer (13 Oct): no voice for the set time → warn 1 minute before (voice + vibration +
     * notification; any voice cancels), then stop like the Stop button. Paused time never counts.
     */
    private fun checkSilenceStop(s: Session, now: Long) {
        if (s.silenceStopMs <= 0) return
        if (s.status != RecordingStatus.RECORDING) {
            s.lastVoiceAtMs = now
            s.silenceWarned = false
            return
        }
        val quiet = now - s.lastVoiceAtMs
        val warnAt = (s.silenceStopMs - SILENCE_WARN_BEFORE_MS).coerceAtLeast(s.silenceStopMs / 2)
        when {
            quiet >= s.silenceStopMs -> mainHandler.post {
                val cur = session
                if (cur === s && cur.status == RecordingStatus.RECORDING) {
                    val min = s.silenceStopMs / 60_000
                    Log.i(TAG, "No voice for $min min: stopping (admin silence timer)")
                    RecorderNotifications.alert(this, "⏹️ Recording stopped", "No voice for $min minutes, so the recording was stopped. The report is being prepared.")
                    stop()
                }
            }
            quiet >= warnAt && !s.silenceWarned -> {
                s.silenceWarned = true
                mainHandler.post {
                    micVoice?.say(MicVoice.Clip.SILENCE_STOP_WARNING)
                    triggerVibration(longArrayOf(0, 300, 150, 300))
                    RecorderNotifications.alert(this, "⏳ Recording will stop in 1 minute", "No voice is being heard. Speak, or tap the app, to keep recording.")
                }
            }
            quiet < warnAt && s.silenceWarned -> s.silenceWarned = false
        }
    }

    /**
     * Safety net for phones that delay plug/unplug events for background apps (e.g. Xiaomi): every second,
     * if we record on an external mic that no longer exists, handle it like an unplug.
     */
    private fun checkExternalMicStillThere(s: Session) {
        if (s.status != RecordingStatus.RECORDING || !MicrophoneRouter.isExternal(s.micWatch.micType)) return
        mainHandler.post {
            val cur = session
            if (cur === s && cur.status == RecordingStatus.RECORDING && MicrophoneRouter.isExternal(cur.micWatch.micType)
                && microphoneRouter.preferredExternal() == null) {
                Log.w(TAG, "External mic missing (no unplug event received) — treating as unplugged")
                if (cur.micWatch.onExternalRemoved(anyExternalLeft = false)) onMicLost(dead = false)
            }
        }
    }

    private fun startHeartbeatLoop() {
        heartbeatJob?.cancel()
        heartbeatJob = serviceScope.launch(Dispatchers.IO) {
            while (isActive) {
                delay(HEARTBEAT_MS)
                val s = session ?: break
                if (s.status == RecordingStatus.FINALIZING) break
                if (!SessionStarter.ensureStarted(applicationContext, s.id)) continue
                val state = heartbeatState(s.status, s.voiceActive)
                val r = apiClient.heartbeat(s.id, state, s.chunkManager.savedVoiceSeconds, s.sessionSeconds.get())
                when (r.getOrNull()) {
                    "discarded" -> withContext(Dispatchers.Main) { discardActiveSession(s) }
                    "finalized" -> Log.w(TAG, "Server already finalized ${s.id}; continuing to record locally")
                    null -> Log.d(TAG, "Heartbeat failed: ${r.exceptionOrNull()?.message}")
                }
            }
        }
    }

    // ── Notifications, wake lock, vibration ─────────────────────────────────────

    private fun refreshNotification() {
        val s = session ?: return
        val content = RecorderNotifications.foregroundContent(
            s.status, s.voiceActive, s.chunkManager.savedVoiceSeconds,
            inCall = s.pauseReason == PauseReason.CALL, micMessage = s.micMessage
        )
        try {
            getSystemService(android.app.NotificationManager::class.java)
                ?.notify(NOTIFICATION_ID, RecorderNotifications.buildForeground(this, content))
        } catch (t: Throwable) {
            Log.w(TAG, "notify: ${t.message}")
        }
    }

    private fun acquireWakeLock() {
        if (wakeLock == null) {
            val pm = getSystemService(Context.POWER_SERVICE) as PowerManager
            wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "ISFRecorder:AudioRecordingWakeLock").apply {
                setReferenceCounted(false)
            }
        }
        renewWakeLock()
    }

    /** Timeout-bounded wake lock; the timer loop renews it, so a dead loop can't hold it forever. */
    private fun renewWakeLock() {
        try { wakeLock?.acquire(WAKE_LOCK_TIMEOUT_MS) } catch (e: Exception) { Log.w(TAG, "wakelock: ${e.message}") }
    }

    private fun releaseWakeLock() {
        try {
            if (wakeLock?.isHeld == true) wakeLock?.release()
        } catch (e: Exception) {
            Log.w(TAG, "Error releasing wakelock: ${e.message}")
        }
        wakeLock = null
    }

    private fun triggerVibration(pattern: LongArray) {
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
                val vm = getSystemService(Context.VIBRATOR_MANAGER_SERVICE) as? android.os.VibratorManager
                vm?.defaultVibrator?.vibrate(android.os.VibrationEffect.createWaveform(pattern, -1))
            } else {
                @Suppress("DEPRECATION")
                val v = getSystemService(Context.VIBRATOR_SERVICE) as? android.os.Vibrator
                v?.vibrate(android.os.VibrationEffect.createWaveform(pattern, -1))
            }
        } catch (e: Exception) {
            Log.w(TAG, "Vibration failed: ${e.message}")
        }
    }
}
