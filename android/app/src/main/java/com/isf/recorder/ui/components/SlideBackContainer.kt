package com.isf.recorder.ui.components

import androidx.activity.compose.BackHandler
import androidx.compose.animation.core.Animatable
import androidx.compose.animation.core.FastOutSlowInEasing
import androidx.compose.animation.core.Spring
import androidx.compose.animation.core.spring
import androidx.compose.animation.core.tween
import androidx.compose.foundation.gestures.detectHorizontalDragGestures
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.offset
import androidx.compose.runtime.Composable
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.alpha
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.unit.IntOffset
import kotlinx.coroutines.launch
import kotlin.math.roundToInt

/**
 * SlideBackContainer
 * Allows users to swipe right from anywhere on the screen (or screen edge)
 * to smoothly slide back to the previous screen. Also wires Android's
 * phone hardware / gesture BackHandler automatically.
 */
@Composable
fun SlideBackContainer(
    onBack: () -> Unit,
    modifier: Modifier = Modifier,
    enabled: Boolean = true,
    swipeThresholdPx: Float = 220f,
    content: @Composable () -> Unit
) {
    // Phone hardware/gesture back handler
    BackHandler(enabled = enabled, onBack = onBack)

    val coroutineScope = rememberCoroutineScope()
    val animatedOffsetX = remember { Animatable(0f) }

    Box(
        modifier = modifier
            .fillMaxSize()
            .offset { IntOffset(animatedOffsetX.value.roundToInt(), 0) }
            .alpha(((1400f - animatedOffsetX.value) / 1400f).coerceIn(0.2f, 1f))
            .pointerInput(enabled) {
                if (!enabled) return@pointerInput
                detectHorizontalDragGestures(
                    onDragEnd = {
                        coroutineScope.launch {
                            try {
                                if (animatedOffsetX.value > swipeThresholdPx) {
                                    // Swiped past threshold: glide off to the right and navigate back
                                    animatedOffsetX.animateTo(
                                        targetValue = 1400f,
                                        animationSpec = tween(durationMillis = 180, easing = FastOutSlowInEasing)
                                    )
                                    onBack()
                                } else {
                                    // Snap back to origin
                                    animatedOffsetX.animateTo(
                                        targetValue = 0f,
                                        animationSpec = spring(
                                            dampingRatio = Spring.DampingRatioMediumBouncy,
                                            stiffness = Spring.StiffnessMediumLow
                                        )
                                    )
                                }
                            } catch (e: Exception) {
                                // Ignore animation cancellation
                            }
                        }
                    },
                    onDragCancel = {
                        coroutineScope.launch {
                            try {
                                animatedOffsetX.animateTo(0f)
                            } catch (e: Exception) { }
                        }
                    },
                    onHorizontalDrag = { change, dragAmount ->
                        // Only consume when clearly a rightward horizontal swipe
                        if (dragAmount > 0 || animatedOffsetX.value > 0) {
                            if (animatedOffsetX.value > 10f || dragAmount > 4f) {
                                change.consume()
                            }
                            val nextOffset = (animatedOffsetX.value + dragAmount).coerceAtLeast(0f)
                            coroutineScope.launch {
                                try {
                                    animatedOffsetX.snapTo(nextOffset)
                                } catch (e: Exception) { }
                            }
                        }
                    }
                )
            }
    ) {
        content()
    }
}
