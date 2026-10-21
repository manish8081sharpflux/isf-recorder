package com.isf.recorder.ui.components

import android.content.Context
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.isf.recorder.data.prefs.UserPreferences
import com.isf.recorder.ui.theme.*
import kotlinx.coroutines.delay

/**
 * One audio player per report screen, created only when the user first taps play (nothing loads
 * before). Shared by the view-mode "▶ Play audio" player and the edit-mode mini bar, so the position
 * carries over while the screen is open; released when the screen closes.
 */
class LazyAudio(private val context: Context, val url: String, private val token: String?, private val deleted: Boolean) {
    var controller by mutableStateOf<AudioPlayerController?>(null)
        private set

    fun open(): AudioPlayerController =
        controller ?: AudioPlayerController(context, url, token, deleted).also { controller = it }

    fun pause() { controller?.pause() }

    fun release() {
        controller?.release()
        controller = null
    }
}

@Composable
fun rememberLazyAudio(url: String, audioDeleted: Boolean = false): LazyAudio {
    val context = LocalContext.current
    val token = remember { UserPreferences(context).token }
    val audio = remember(url, audioDeleted) { LazyAudio(context.applicationContext, url, token, audioDeleted) }
    DisposableEffect(audio) { onDispose { audio.release() } }
    val c = audio.controller
    LaunchedEffect(c) {
        if (c != null) while (true) { c.tick(); delay(250) }
    }
    return audio
}

/**
 * Report views (v2.1): the audio is optional and collapsed. Only a "▶ Play audio" chip shows; nothing
 * loads until it is tapped. Tapping expands the full [AudioPlayer]; "✕ Hide" collapses it and pauses,
 * keeping the position while this screen is open. Every report starts collapsed.
 */
@Composable
fun CollapsibleAudioPlayer(audio: LazyAudio, title: String, fallbackDurationSec: Int, modifier: Modifier = Modifier) {
    var expanded by remember(audio) { mutableStateOf(false) }
    Column(modifier.fillMaxWidth()) {
        val c = audio.controller
        if (!expanded || c == null) {
            OutlinedButton(
                onClick = { audio.open(); expanded = true },
                shape = RoundedCornerShape(20.dp),
                border = BorderStroke(1.dp, JomiMintBorder),
                contentPadding = PaddingValues(horizontal = 14.dp, vertical = 4.dp)
            ) { Text("▶ Play audio", color = JomiTeal, fontWeight = FontWeight.SemiBold, fontSize = 13.sp) }
        } else {
            JomiCard(padding = 12.dp) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Text("Audio", color = JomiTeal, fontWeight = FontWeight.Bold, fontSize = 12.sp, modifier = Modifier.weight(1f))
                    TextButton(onClick = { c.pause(); expanded = false },
                        contentPadding = PaddingValues(horizontal = 8.dp, vertical = 0.dp)) {
                        Text("✕ Hide", color = JomiTextMuted, fontSize = 12.sp)
                    }
                }
                AudioPlayer(controller = c, downloadName = title, fallbackDurationSec = fallbackDurationSec)
            }
        }
    }
}

private val SPEEDS = listOf(1.0f, 1.25f, 1.5f, 2.0f)

private fun mmss(ms: Long): String {
    val t = (ms / 1000).coerceAtLeast(0)
    return if (t >= 3600) "%d:%02d:%02d".format(t / 3600, (t % 3600) / 60, t % 60) else "%02d:%02d".format(t / 60, t % 60)
}

/**
 * Edit mode (v2.1): a one-line player pinned above the editor so the coach can listen and type.
 * ▶/❚❚ · ⟲10 · 10⟳ · speed chip (1.0→1.25→1.5→2.0) · mm:ss / mm:ss. Nothing loads until ▶.
 * Tapping the time expands the full player (seek bar, download); "Collapse" returns to the bar.
 */
@Composable
fun MiniAudioBar(audio: LazyAudio, title: String, fallbackDurationSec: Int, modifier: Modifier = Modifier) {
    var expanded by remember(audio) { mutableStateOf(false) }
    val c = audio.controller
    if (expanded && c != null) {
        JomiCard(padding = 10.dp, modifier = modifier) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text("Audio", color = JomiTeal, fontWeight = FontWeight.Bold, fontSize = 12.sp, modifier = Modifier.weight(1f))
                TextButton(onClick = { expanded = false }, contentPadding = PaddingValues(horizontal = 8.dp, vertical = 0.dp)) {
                    Text("Collapse", color = JomiTextMuted, fontSize = 12.sp)
                }
            }
            AudioPlayer(controller = c, downloadName = title, fallbackDurationSec = fallbackDurationSec)
        }
        return
    }
    Surface(
        color = JomiMint,
        shape = RoundedCornerShape(14.dp),
        border = BorderStroke(1.dp, JomiMintBorder),
        modifier = modifier.fillMaxWidth()
    ) {
      Column {
        Row(Modifier.padding(horizontal = 6.dp).height(46.dp), verticalAlignment = Alignment.CenterVertically) {
            TextButton(onClick = { audio.open().togglePlay() }, contentPadding = PaddingValues(0.dp),
                modifier = Modifier.size(40.dp)) {
                Text(if (c?.isPlaying == true) "❚❚" else "▶", color = JomiTeal, fontSize = 16.sp, fontWeight = FontWeight.Bold)
            }
            TextButton(onClick = { c?.skipBack() }, enabled = c != null, contentPadding = PaddingValues(0.dp),
                modifier = Modifier.size(40.dp)) { Text("⟲10", color = JomiTeal, fontSize = 12.sp) }
            TextButton(onClick = { c?.skipForward() }, enabled = c != null, contentPadding = PaddingValues(0.dp),
                modifier = Modifier.size(40.dp)) { Text("10⟳", color = JomiTeal, fontSize = 12.sp) }
            val speed = c?.speed ?: 1f
            Surface(
                shape = RoundedCornerShape(10.dp), color = JomiCard, border = BorderStroke(1.dp, JomiMintBorder),
                modifier = Modifier.clickable(enabled = c != null) {
                    c?.changeSpeed(SPEEDS[(SPEEDS.indexOf(speed).coerceAtLeast(0) + 1) % SPEEDS.size])
                }
            ) {
                Text(if (speed == 1f) "1.0×" else "${speed}×", color = JomiTeal, fontSize = 12.sp,
                    fontWeight = FontWeight.SemiBold, modifier = Modifier.padding(horizontal = 8.dp, vertical = 3.dp))
            }
            val dur = c?.durationMs?.takeIf { it > 0 } ?: fallbackDurationSec * 1000L
            Text(
                text = when {
                    c == null -> "Tap ▶ to play"
                    c.error != null -> c.error ?: ""
                    else -> "${mmss(c.positionMs)} / ${mmss(dur)}"
                },
                color = if (c?.error != null) RecordRed else JomiTextMuted, fontSize = 12.sp, maxLines = 1,
                modifier = Modifier.weight(1f).padding(start = 8.dp)
                    .clickable(enabled = c != null) { expanded = true }
            )
        }
        // Thin seek bar under the buttons (shown once audio is loaded): drag to go back / forward
        val total = c?.durationMs?.takeIf { it > 0 } ?: fallbackDurationSec * 1000L
        if (c != null && c.error == null && total > 0) {
            var dragging by remember(c) { mutableStateOf<Float?>(null) }
            val frac = (dragging ?: (c.positionMs.toFloat() / total).coerceIn(0f, 1f))
            Slider(
                value = frac,
                onValueChange = { dragging = it },
                onValueChangeFinished = { dragging?.let { c.seekTo((it * total).toLong()) }; dragging = null },
                colors = SliderDefaults.colors(thumbColor = JomiGreen, activeTrackColor = JomiGreen,
                    inactiveTrackColor = JomiMintBorder),
                modifier = Modifier.fillMaxWidth().height(22.dp).padding(horizontal = 10.dp)
            )
        }
      }
    }
}
