package com.isf.recorder.ui.components

import androidx.compose.animation.core.*
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.CornerRadius
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.unit.dp
import com.isf.recorder.ui.theme.CyanAccent
import com.isf.recorder.ui.theme.EmeraldPrimary
import com.isf.recorder.ui.theme.RecordRed
import com.isf.recorder.ui.theme.JomiGreen
import com.isf.recorder.ui.theme.JomiTeal
import com.isf.recorder.ui.theme.JomiTextMuted
import kotlin.random.Random

@Composable
fun WaveformVisualizer(
    isRecording: Boolean,
    isPaused: Boolean,
    amplitude: Float,
    barCount: Int = 32,
    modifier: Modifier = Modifier.fillMaxWidth().height(72.dp)
) {
    val infiniteTransition = rememberInfiniteTransition(label = "pulse")
    val phase by infiniteTransition.animateFloat(
        initialValue = 0f,
        targetValue = 1f,
        animationSpec = infiniteRepeatable(
            animation = tween(1200, easing = LinearEasing),
            repeatMode = RepeatMode.Restart
        ),
        label = "phase"
    )

    // Jomi v2: teal/emerald bars (mockup); muted while paused or idle
    val activeColor = if (isRecording && !isPaused) JomiGreen else JomiTextMuted.copy(alpha = 0.45f)
    val gradient = Brush.verticalGradient(
        colors = listOf(activeColor, if (isRecording && !isPaused) JomiTeal else activeColor)
    )

    Canvas(modifier = modifier) {
        val totalWidth = size.width
        val maxHeight = size.height
        val barWidth = (totalWidth / (barCount * 1.5f)).coerceAtLeast(4f)
        val spacing = barWidth * 0.5f

        for (i in 0 until barCount) {
            val x = i * (barWidth + spacing) + spacing

            val targetHeightFraction = if (isRecording && !isPaused) {
                val waveFactor = kotlin.math.sin((i / barCount.toFloat() * 6.28f) + phase * 6.28f) * 0.3f
                (amplitude * 1.2f + waveFactor + 0.15f).coerceIn(0.1f, 1.0f)
            } else if (isPaused) {
                0.2f
            } else {
                0.08f
            }

            val barHeight = maxHeight * targetHeightFraction
            val y = (maxHeight - barHeight) / 2f

            drawRoundRect(
                brush = gradient,
                topLeft = Offset(x, y),
                size = Size(barWidth, barHeight),
                cornerRadius = CornerRadius(barWidth / 2f, barWidth / 2f)
            )
        }
    }
}
