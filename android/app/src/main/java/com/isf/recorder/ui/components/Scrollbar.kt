package com.isf.recorder.ui.components

import androidx.compose.foundation.ScrollState
import androidx.compose.foundation.background
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.gestures.detectVerticalDragGestures
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyListState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.layout.onSizeChanged
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.unit.dp
import com.isf.recorder.ui.theme.JomiBorder
import com.isf.recorder.ui.theme.JomiGreen
import kotlinx.coroutines.launch

/**
 * Draggable scroll bar on the right of long texts (21 Oct): the thumb's size shows how long the text is, its place
 * shows how far you have read, and dragging it (or tapping the track) jumps there. Always visible while the text
 * is longer than the screen. Put it inside the Box that holds the scrolling content:
 *   Box { Column(Modifier.verticalScroll(state)) {…}; ScrollbarFor(state) }
 * Keep ~12 dp of end padding on the content so the bar does not cover text.
 */

/** Pure math (unit-tested): thumb top and height in px for a track of [trackPx]. */
fun thumbGeometry(positionFraction: Float, visibleFraction: Float, trackPx: Float, minThumbPx: Float): Pair<Float, Float> {
    val height = (visibleFraction.coerceIn(0f, 1f) * trackPx).coerceIn(minOf(minThumbPx, trackPx), trackPx)
    val top = positionFraction.coerceIn(0f, 1f) * (trackPx - height)
    return top to height
}

/** Where a finger at [yPx] on the track wants the text to be (0 = top, 1 = end), centring the thumb on the finger. */
fun fractionAt(yPx: Float, thumbPx: Float, trackPx: Float): Float =
    if (trackPx <= thumbPx) 0f else ((yPx - thumbPx / 2f) / (trackPx - thumbPx)).coerceIn(0f, 1f)

@Composable
private fun ScrollbarTrack(
    positionFraction: Float,
    visibleFraction: Float,
    onJumpTo: (Float) -> Unit,
    modifier: Modifier = Modifier
) {
    val density = LocalDensity.current
    val minThumbPx = with(density) { 36.dp.toPx() }
    var trackPx by remember { mutableFloatStateOf(0f) }
    var dragging by remember { mutableStateOf(false) }
    val (top, height) = thumbGeometry(positionFraction, visibleFraction, trackPx, minThumbPx)
    val jump by rememberUpdatedState(onJumpTo)
    val thumbPx by rememberUpdatedState(height)        // gesture handlers live across recompositions

    Box(
        modifier
            .width(18.dp)                       // finger-sized touch area; the drawn bar is thinner
            .fillMaxHeight()
            .padding(vertical = 4.dp)
            .onSizeChanged { trackPx = it.height.toFloat() }
            .pointerInput(Unit) {
                detectTapGestures { o -> jump(fractionAt(o.y, thumbPx, trackPx)) }
            }
            .pointerInput(Unit) {
                detectVerticalDragGestures(
                    onDragStart = { dragging = true },
                    onDragEnd = { dragging = false },
                    onDragCancel = { dragging = false }
                ) { change, _ ->
                    change.consume()
                    jump(fractionAt(change.position.y, thumbPx, trackPx))
                }
            },
        contentAlignment = Alignment.TopCenter
    ) {
        Box(Modifier.fillMaxHeight().width(3.dp).background(JomiBorder.copy(alpha = 0.6f), RoundedCornerShape(2.dp)))
        Box(
            Modifier
                .offset(y = with(density) { top.toDp() })
                .width(if (dragging) 8.dp else 6.dp)
                .height(with(density) { height.toDp() })
                .background(JomiGreen.copy(alpha = if (dragging) 1f else 0.75f), RoundedCornerShape(4.dp))
        )
    }
}

/** Scroll bar for a Column with verticalScroll([state]). */
@Composable
fun BoxScope.ScrollbarFor(state: ScrollState) {
    val scope = rememberCoroutineScope()
    if (state.maxValue <= 0 || state.maxValue == Int.MAX_VALUE) return
    val viewport = state.viewportSize.toFloat()
    val total = viewport + state.maxValue
    Box(Modifier.matchParentSize(), contentAlignment = Alignment.TopEnd) {
        ScrollbarTrack(
            positionFraction = state.value / state.maxValue.toFloat(),
            visibleFraction = if (total > 0) viewport / total else 1f,
            onJumpTo = { f -> scope.launch { state.scrollTo((f * state.maxValue).toInt()) } }
        )
    }
}

/**
 * Scroll bar for a LazyColumn. Item heights are learned as items appear (unknown ones count as the average),
 * so the bar is right for chats where one answer is much longer than another.
 */
@Composable
fun BoxScope.ScrollbarFor(state: LazyListState) {
    val scope = rememberCoroutineScope()
    val sizes = remember(state) { mutableMapOf<Int, Int>() }
    val info = state.layoutInfo
    val visible = info.visibleItemsInfo
    if (visible.isEmpty() || !(state.canScrollForward || state.canScrollBackward)) return
    visible.forEach { sizes[it.index] = it.size }
    val count = info.totalItemsCount
    val avg = sizes.values.average().toFloat()
    val gap = info.mainAxisItemSpacing
    fun size(i: Int) = (sizes[i]?.toFloat() ?: avg) + gap
    val total = (0 until count).sumOf { size(it).toDouble() }.toFloat()
    val viewport = (info.viewportEndOffset - info.viewportStartOffset).toFloat()
    val scrolled = (0 until state.firstVisibleItemIndex).sumOf { size(it).toDouble() }.toFloat() + state.firstVisibleItemScrollOffset
    val maxScroll = (total - viewport).coerceAtLeast(1f)
    Box(Modifier.matchParentSize(), contentAlignment = Alignment.TopEnd) {
        ScrollbarTrack(
            positionFraction = if (!state.canScrollForward) 1f else scrolled / maxScroll,
            visibleFraction = viewport / total.coerceAtLeast(1f),
            onJumpTo = { f ->
                var target = f * maxScroll
                var index = 0
                while (index < count - 1 && target >= size(index)) { target -= size(index); index++ }
                scope.launch { state.scrollToItem(index, target.toInt()) }
            }
        )
    }
}
