package com.isf.recorder.ui.components

import android.content.Context
import androidx.annotation.OptIn
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.media3.common.AudioAttributes
import androidx.media3.common.C
import androidx.media3.common.MediaItem
import androidx.media3.common.PlaybackException
import androidx.media3.common.Player
import androidx.media3.common.util.UnstableApi
import androidx.media3.datasource.HttpDataSource
import androidx.media3.datasource.okhttp.OkHttpDataSource
import androidx.media3.exoplayer.ExoPlayer
import androidx.media3.exoplayer.source.DefaultMediaSourceFactory
import com.isf.recorder.R
import com.isf.recorder.data.api.ISFApiClient
import com.isf.recorder.data.prefs.UserPreferences
import com.isf.recorder.ui.theme.*
import kotlinx.coroutines.delay

/**
 * Owns one ExoPlayer streaming an authenticated URL through the shared OkHttp client
 * (Authorization: Bearer header — never ?token=). Exposes Compose state for the UI and
 * lets other UI (e.g. transcript segments) seek and play. Main thread only.
 */
@OptIn(UnstableApi::class)
private const val MAX_NOT_READY_RETRIES = 40      // ~2 min
private const val NOT_READY_RETRY_MS = 3_000L

class AudioPlayerController(
    context: Context,
    val url: String,
    private val token: String?,
    /** audio_deleted_at is set: the file went to the old 7-day cleanup (changes the 410 wording). */
    private val audioDeleted: Boolean = false
) {
    private val player: ExoPlayer

    var isPlaying by mutableStateOf(false)
        private set
    var isBuffering by mutableStateOf(false)
        private set
    var isReady by mutableStateOf(false)
        private set
    var positionMs by mutableLongStateOf(0L)
        private set
    var durationMs by mutableLongStateOf(0L)
        private set
    var speed by mutableFloatStateOf(1f)
        private set
    var error by mutableStateOf<String?>(null)
        private set
    private var released = false
    private var notReadyRetries = 0
    private val mainHandler = android.os.Handler(android.os.Looper.getMainLooper())

    private val listener = object : Player.Listener {
        override fun onIsPlayingChanged(playing: Boolean) {
            isPlaying = playing
        }

        override fun onPlaybackStateChanged(state: Int) {
            isBuffering = state == Player.STATE_BUFFERING
            if (state == Player.STATE_READY) {
                isReady = true
                error = null
                durationMs = player.duration.takeIf { it != C.TIME_UNSET && it > 0 } ?: durationMs
            }
            if (state == Player.STATE_ENDED) {
                player.pause()
                player.seekTo(0)
                positionMs = 0
            }
        }

        override fun onPlayerError(e: PlaybackException) {
            isPlaying = false
            isBuffering = false
            error = describe(e)
            // 404 = the server is still stitching the audio after Stop: try again by itself
            if (httpCode(e) == 404 && notReadyRetries < MAX_NOT_READY_RETRIES) {
                notReadyRetries++
                mainHandler.postDelayed({ if (!released && error != null) retry() }, NOT_READY_RETRY_MS)
            }
        }
    }

    init {
        val dataSourceFactory = OkHttpDataSource.Factory(ISFApiClient.sharedOkHttpClient).apply {
            if (!token.isNullOrBlank()) setDefaultRequestProperties(mapOf("Authorization" to "Bearer $token"))
        }
        player = ExoPlayer.Builder(context.applicationContext)
            .setMediaSourceFactory(DefaultMediaSourceFactory(dataSourceFactory))
            .setSeekBackIncrementMs(10_000)
            .setSeekForwardIncrementMs(10_000)
            .build()
        player.setAudioAttributes(
            AudioAttributes.Builder().setUsage(C.USAGE_MEDIA).setContentType(C.AUDIO_CONTENT_TYPE_SPEECH).build(),
            /* handleAudioFocus = */ true
        )
        player.addListener(listener)
        player.setMediaItem(MediaItem.fromUri(url))
        player.prepare()
    }

    private fun httpCode(e: PlaybackException): Int? {
        var cause: Throwable? = e
        while (cause != null) {
            if (cause is HttpDataSource.InvalidResponseCodeException) return cause.responseCode
            cause = cause.cause
        }
        return null
    }

    private fun describe(e: PlaybackException): String {
        var cause: Throwable? = e
        while (cause != null) {
            if (cause is HttpDataSource.InvalidResponseCodeException) {
                return when (cause.responseCode) {
                    404 -> "Audio is still being saved on the server…"
                    401, 403 -> "You are not allowed to play this audio."
                    410 -> audioUnavailableMessage(audioDeleted)
                    else -> "Server error ${cause.responseCode} while loading audio."
                }
            }
            if (cause is HttpDataSource.HttpDataSourceException) {
                return "Can't reach the server. Check your internet connection."
            }
            cause = cause.cause
        }
        return "Could not play this audio (${e.errorCodeName})."
    }

    /** Called every ~250 ms by the UI to refresh time and duration. */
    fun tick() {
        if (released) return
        positionMs = player.currentPosition.coerceAtLeast(0)
        val d = player.duration
        if (d != C.TIME_UNSET && d > 0) durationMs = d
    }

    fun togglePlay() {
        if (released) return
        if (error != null) retry()
        if (player.isPlaying) player.pause() else player.play()
    }

    fun play() {
        if (!released) player.play()
    }

    fun pause() {
        if (!released) player.pause()
    }

    fun seekTo(ms: Long) {
        if (released) return
        val target = if (durationMs > 0) ms.coerceIn(0, durationMs) else ms.coerceAtLeast(0)
        player.seekTo(target)
        positionMs = target
    }

    /** Seek (e.g. from a transcript line) and start playing. */
    fun playFrom(ms: Long) {
        seekTo(ms)
        play()
    }

    fun skipBack() = seekTo(player.currentPosition - 10_000)
    fun skipForward() = seekTo(player.currentPosition + 10_000)

    fun changeSpeed(value: Float) {
        if (released) return
        speed = value
        player.setPlaybackSpeed(value)
    }

    fun retry() {
        if (released) return
        error = null
        player.prepare()
    }

    fun release() {
        if (released) return
        released = true
        mainHandler.removeCallbacksAndMessages(null)
        player.removeListener(listener)
        player.release()
    }
}

/** Remembers a controller for [url]; releases the player when it leaves composition or the URL changes. */
@Composable
fun rememberAudioPlayerController(url: String, audioDeleted: Boolean = false): AudioPlayerController {
    val context = LocalContext.current
    val token = remember { UserPreferences(context).token }
    val controller = remember(url, audioDeleted) { AudioPlayerController(context, url, token, audioDeleted) }
    DisposableEffect(controller) {
        onDispose { controller.release() }
    }
    LaunchedEffect(controller) {
        while (true) {
            controller.tick()
            delay(250)
        }
    }
    return controller
}

/**
 * Mockup "Audio playback" block: title + M4A download, seek bar with times,
 * ⟲10 / Play / 10⟳, speed chips. Shows errors with a Retry instead of failing silently.
 */
@Composable
fun AudioPlayer(
    controller: AudioPlayerController,
    downloadName: String,
    modifier: Modifier = Modifier,
    title: String = "Audio playback",
    fallbackDurationSec: Int = 0
) {
    val context = LocalContext.current
    val token = remember { UserPreferences(context).token }
    var dragValue by remember { mutableStateOf<Float?>(null) }
    val duration = if (controller.durationMs > 0) controller.durationMs else fallbackDurationSec * 1000L

    Column(modifier = modifier.fillMaxWidth()) {
        Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.fillMaxWidth()) {
            Text(title, color = MaterialTheme.colorScheme.onBackground, fontSize = 14.sp, modifier = Modifier.weight(1f))
            if (controller.isBuffering) {
                CircularProgressIndicator(modifier = Modifier.size(14.dp), strokeWidth = 2.dp, color = JomiGreen)
                Spacer(Modifier.width(8.dp))
            }
            OutlinedButton(
                onClick = { downloadAuthenticated(context, controller.url, token, "$downloadName.m4a") },
                shape = RoundedCornerShape(10.dp),
                border = BorderStroke(1.dp, JomiBorder),
                contentPadding = PaddingValues(horizontal = 12.dp, vertical = 6.dp),
                colors = ButtonDefaults.outlinedButtonColors(contentColor = JomiTextMuted)
            ) {
                Icon(painterResource(R.drawable.ic_download), contentDescription = null, modifier = Modifier.size(16.dp))
                Spacer(Modifier.width(6.dp))
                Text("M4A", fontSize = 13.sp)
            }
        }

        Spacer(Modifier.height(6.dp))
        val fraction = if (duration > 0) (controller.positionMs.toFloat() / duration).coerceIn(0f, 1f) else 0f
        Slider(
            value = dragValue ?: fraction,
            onValueChange = { dragValue = it },
            onValueChangeFinished = {
                dragValue?.let { controller.seekTo((it * duration).toLong()) }
                dragValue = null
            },
            enabled = duration > 0,
            colors = SliderDefaults.colors(
                thumbColor = Color.White,
                activeTrackColor = JomiGreen,
                inactiveTrackColor = JomiBorder
            ),
            modifier = Modifier.fillMaxWidth().height(28.dp)
        )
        Row(modifier = Modifier.fillMaxWidth()) {
            val shownPos = dragValue?.let { (it * duration).toLong() } ?: controller.positionMs
            Text(clockShort(shownPos), color = JomiTextMuted, fontSize = 12.sp, modifier = Modifier.weight(1f))
            Text(if (duration > 0) clockShort(duration) else "--:--", color = JomiTextMuted, fontSize = 12.sp)
        }

        Spacer(Modifier.height(10.dp))
        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.spacedBy(12.dp),
            verticalAlignment = Alignment.CenterVertically
        ) {
            SkipButton(R.drawable.ic_replay_10, "Back 10 seconds", controller::skipBack)
            Button(
                onClick = { controller.togglePlay() },
                shape = RoundedCornerShape(14.dp),
                colors = ButtonDefaults.buttonColors(containerColor = JomiGreen, contentColor = Color.White),
                modifier = Modifier.weight(1f).height(56.dp)
            ) {
                Icon(
                    painterResource(if (controller.isPlaying) R.drawable.ic_pause else R.drawable.ic_play),
                    contentDescription = null,
                    modifier = Modifier.size(22.dp)
                )
                Spacer(Modifier.width(6.dp))
                Text(if (controller.isPlaying) "Pause" else "Play", fontSize = 16.sp, fontWeight = FontWeight.Bold)
            }
            SkipButton(R.drawable.ic_forward_10, "Forward 10 seconds", controller::skipForward)
        }

        Spacer(Modifier.height(10.dp))
        Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Text("Speed", color = JomiTextMuted, fontSize = 13.sp)
            listOf(1.0f, 1.25f, 1.5f, 2.0f).forEach { s ->
                val selected = controller.speed == s
                Surface(
                    onClick = { controller.changeSpeed(s) },
                    shape = RoundedCornerShape(10.dp),
                    color = if (selected) JomiMint else JomiSegmentBg.copy(alpha = 0.6f),
                    border = BorderStroke(1.dp, if (selected) JomiGreen else Color.Transparent),
                    modifier = Modifier.weight(1f).height(36.dp)
                ) {
                    Box(contentAlignment = Alignment.Center) {
                        Text(
                            text = if (s == 1f || s == 2f) "${s}x" else "${s}x",
                            color = if (selected) JomiTeal else JomiTextMuted,
                            fontSize = 13.sp,
                            fontWeight = if (selected) FontWeight.Bold else FontWeight.Normal
                        )
                    }
                }
            }
        }

        val err = controller.error
        if (err != null) {
            Spacer(Modifier.height(10.dp))
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(err, color = RecordRed, fontSize = 12.sp, modifier = Modifier.weight(1f))
                TextButton(onClick = { controller.retry() }) { Text("Retry", color = JomiTeal, fontWeight = FontWeight.SemiBold) }
            }
        }
    }
}

@Composable
private fun SkipButton(icon: Int, description: String, onClick: () -> Unit) {
    Surface(
        onClick = onClick,
        shape = RoundedCornerShape(14.dp),
        color = Color.White,
        border = BorderStroke(1.dp, JomiBorder),
        modifier = Modifier.size(width = 60.dp, height = 56.dp)
    ) {
        Row(horizontalArrangement = Arrangement.Center, verticalAlignment = Alignment.CenterVertically) {
            Icon(painterResource(icon), contentDescription = description, tint = JomiTextMuted, modifier = Modifier.size(18.dp))
            Column(horizontalAlignment = Alignment.CenterHorizontally) {
                Text("10", color = JomiTextMuted, fontSize = 12.sp, lineHeight = 13.sp)
                Text("sec", color = JomiTextMuted, fontSize = 10.sp, lineHeight = 11.sp)
            }
        }
    }
}
