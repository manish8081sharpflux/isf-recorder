package com.isf.recorder.ui.components

import android.annotation.SuppressLint
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.asPaddingValues
import androidx.compose.foundation.layout.navigationBars
import androidx.compose.runtime.Composable
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.platform.LocalView
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import androidx.core.view.ViewCompat
import androidx.core.view.WindowInsetsCompat

/**
 * Height of the phone's ◁ ○ ☰ bar for content in popups (22 Oct). A bottom sheet runs in its own window and on some
 * phones (seen on a Redmi with 3-button navigation) it reports 0 for the navigation bar, so Save / Cancel ended up
 * UNDER the phone buttons and could not be tapped. Takes the largest of: Compose's value, the window's real insets,
 * and — only if both say 0 — the system's navigation bar height.
 */
@SuppressLint("DiscouragedApi", "InternalInsetResource")
@Composable
fun navBarBottom(): Dp {
    val density = LocalDensity.current
    val compose = WindowInsets.navigationBars.asPaddingValues().calculateBottomPadding()
    val view = LocalView.current
    val viewPx = ViewCompat.getRootWindowInsets(view)?.getInsets(WindowInsetsCompat.Type.navigationBars())?.bottom ?: 0
    val fromView = with(density) { viewPx.toDp() }
    val best = maxOf(compose, fromView)
    if (best > 0.dp) return best
    val res = view.resources
    val id = res.getIdentifier("navigation_bar_height", "dimen", "android")
    return if (id > 0) with(density) { res.getDimensionPixelSize(id).toDp() } else 0.dp
}
