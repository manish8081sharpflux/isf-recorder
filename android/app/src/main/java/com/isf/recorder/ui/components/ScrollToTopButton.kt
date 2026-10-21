package com.isf.recorder.ui.components

import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.scaleIn
import androidx.compose.animation.scaleOut
import androidx.compose.foundation.ScrollState
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material3.SmallFloatingActionButton
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.derivedStateOf
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.isf.recorder.ui.theme.JomiGreen
import kotlinx.coroutines.launch
import androidx.compose.ui.graphics.Color

/** Small floating "↑": appears once the page is scrolled down, scrolls smoothly back to the top. */
@Composable
fun ScrollToTopButton(state: ScrollState, modifier: Modifier = Modifier) {
    val scope = rememberCoroutineScope()
    val thresholdPx = with(LocalDensity.current) { 400.dp.toPx() }
    val show by remember(state) { derivedStateOf { state.value > thresholdPx } }
    AnimatedVisibility(visible = show, enter = fadeIn() + scaleIn(), exit = fadeOut() + scaleOut(), modifier = modifier) {
        SmallFloatingActionButton(
            onClick = { scope.launch { state.animateScrollTo(0) } },
            shape = CircleShape,
            containerColor = JomiGreen,
            contentColor = Color.White,
            modifier = Modifier.size(44.dp).semantics { contentDescription = "Scroll to top" }
        ) { Text("↑", fontSize = 20.sp, fontWeight = FontWeight.Bold) }
    }
}
