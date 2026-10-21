package com.isf.recorder.ui.components

import android.content.Context
import android.media.AudioDeviceCallback
import android.media.AudioDeviceInfo
import android.media.AudioManager
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.os.VibrationEffect
import android.os.Vibrator
import android.os.VibratorManager
import androidx.compose.animation.core.*
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.alpha
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.isf.recorder.data.model.MicType
import com.isf.recorder.service.MicrophoneRouter
import com.isf.recorder.ui.theme.*
import kotlinx.coroutines.delay

private val MicOkGreen = Color(0xFF16A34A)

/** The external mic (USB-C / wired) that is plugged in right now, or null. Updates on plug / unplug. */
@Composable
fun rememberExternalMic(): MicType? {
    val context = LocalContext.current
    val am = remember { context.getSystemService(Context.AUDIO_SERVICE) as AudioManager }
    fun current(): MicType? = am.getDevices(AudioManager.GET_DEVICES_INPUTS)
        .firstOrNull { MicrophoneRouter.isExternalType(it.type) }
        ?.let { MicrophoneRouter.micTypeOf(it.type) }
    var mic by remember { mutableStateOf(current()) }
    DisposableEffect(am) {
        val cb = object : AudioDeviceCallback() {
            override fun onAudioDevicesAdded(addedDevices: Array<out AudioDeviceInfo>?) { mic = current() }
            override fun onAudioDevicesRemoved(removedDevices: Array<out AudioDeviceInfo>?) { mic = current() }
        }
        am.registerAudioDeviceCallback(cb, Handler(Looper.getMainLooper()))
        onDispose { am.unregisterAudioDeviceCallback(cb) }
    }
    return mic
}

private fun vibrateOnce(context: Context) {
    try {
        val v = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S)
            (context.getSystemService(Context.VIBRATOR_MANAGER_SERVICE) as VibratorManager).defaultVibrator
        else @Suppress("DEPRECATION") (context.getSystemService(Context.VIBRATOR_SERVICE) as Vibrator)
        v.vibrate(VibrationEffect.createOneShot(150, VibrationEffect.DEFAULT_AMPLITUDE))
    } catch (_: Exception) {}
}

/**
 * Instruction 1: a prominent mic line on Home. Green "USB-C mic connected" (flashes for a few seconds
 * and vibrates when the mic is plugged in) or orange "External mic not connected — only phone mic working".
 * [activeMic] = the mic the running recording uses (null when not recording).
 */
@Composable
fun MicStatusBar(externalMic: MicType?, activeMic: MicType?, modifier: Modifier = Modifier) {
    val context = LocalContext.current
    val connected = if (activeMic != null) MicrophoneRouter.isExternal(activeMic) else externalMic != null
    val type = activeMic?.takeIf { MicrophoneRouter.isExternal(it) } ?: externalMic

    // Flash for ~4 s whenever the mic becomes connected (not on first show).
    var flashing by remember { mutableStateOf(false) }
    var lastConnected by remember { mutableStateOf<Boolean?>(null) }
    LaunchedEffect(connected) {
        if (lastConnected == false && connected) {
            vibrateOnce(context)
            flashing = true
            delay(4_000)
            flashing = false
        }
        lastConnected = connected
    }
    val pulse = rememberInfiniteTransition(label = "micFlash")
    val alpha by pulse.animateFloat(1f, 0.25f, infiniteRepeatable(tween(350), RepeatMode.Reverse), label = "a")

    val color = if (connected) MicOkGreen else WarningOrange
    val label = when {
        connected && type == MicType.WIRED -> "🎧  Wired mic connected"
        connected -> "🎧  USB-C mic connected"
        activeMic != null -> "📱  Recording with the phone mic — external mic not connected"
        else -> "📱  External mic not connected — only phone mic working"
    }
    Surface(
        color = color.copy(alpha = 0.12f),
        shape = RoundedCornerShape(14.dp),
        border = BorderStroke(2.dp, color),
        modifier = modifier.fillMaxWidth().then(if (flashing) Modifier.alpha(alpha) else Modifier)
    ) {
        Text(
            label, color = color, fontWeight = FontWeight.Bold, fontSize = 15.sp,
            modifier = Modifier.padding(horizontal = 14.dp, vertical = 12.dp)
        )
    }
}

/**
 * Instruction 3: shown while the recording is paused because the external mic was unplugged or gives
 * no sound. Plugging it back in resumes automatically; otherwise the coach chooses.
 */
@Composable
fun MicLostPrompt(message: String?, onUsePhoneMic: () -> Unit, onStop: () -> Unit, modifier: Modifier = Modifier) {
    val pulse = rememberInfiniteTransition(label = "micLost")
    val border by pulse.animateFloat(1f, 0.3f, infiniteRepeatable(tween(500), RepeatMode.Reverse), label = "b")
    val dead = message?.contains("no sound") == true
    Surface(
        color = RecordRed.copy(alpha = 0.08f),
        shape = RoundedCornerShape(16.dp),
        border = BorderStroke(3.dp, RecordRed.copy(alpha = border)),
        modifier = modifier.fillMaxWidth()
    ) {
        Column(Modifier.padding(16.dp), horizontalAlignment = Alignment.CenterHorizontally) {
            Text(if (dead) "🔇  Mic not working" else "🔌  Mic disconnected", color = RecordRed,
                fontWeight = FontWeight.ExtraBold, fontSize = 20.sp)
            Spacer(Modifier.height(4.dp))
            Text("Recording is paused", color = RecordRed, fontWeight = FontWeight.SemiBold, fontSize = 14.sp)
            Spacer(Modifier.height(8.dp))
            Text(
                if (dead) "Check the mic cable. Plug the mic in again to continue automatically."
                else "Plug the mic back in — recording continues automatically.",
                fontSize = 14.sp, color = MaterialTheme.colorScheme.onBackground
            )
            Spacer(Modifier.height(12.dp))
            Button(
                onClick = onUsePhoneMic,
                colors = ButtonDefaults.buttonColors(containerColor = WarningOrange),
                modifier = Modifier.fillMaxWidth()
            ) { Text("📱  Continue with phone mic", fontWeight = FontWeight.Bold) }
            Spacer(Modifier.height(6.dp))
            OutlinedButton(onClick = onStop, border = BorderStroke(1.dp, RecordRed), modifier = Modifier.fillMaxWidth()) {
                Text("⏹  Stop recording", color = RecordRed)
            }
        }
    }
}

/** Instruction 2: asked before recording when no external mic is connected. */
@Composable
fun PhoneMicConfirmDialog(onConfirm: () -> Unit, onDismiss: () -> Unit) {
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("External mic not connected", fontWeight = FontWeight.Bold) },
        text = { Text("Only the phone mic is working. Continue recording with the phone mic?", fontSize = 15.sp) },
        confirmButton = {
            Button(onClick = onConfirm, colors = ButtonDefaults.buttonColors(containerColor = WarningOrange)) { Text("Yes, continue") }
        },
        dismissButton = { TextButton(onClick = onDismiss) { Text("Cancel") } }
    )
}
