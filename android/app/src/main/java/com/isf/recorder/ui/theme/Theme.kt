package com.isf.recorder.ui.theme

import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.staticCompositionLocalOf
import androidx.compose.ui.graphics.Color

// ── Extended theme colors not covered by Material's colorScheme ─────────────────
data class ExtendedColors(
    val cardBorder: Color,
    val surfaceElevated: Color,
    val textMuted: Color,
    val divider: Color,
    val bgDark: Color  // used for text-on-primary-button (always dark)
)

val LocalExtendedColors = staticCompositionLocalOf {
    ExtendedColors(
        cardBorder = CardBorder,
        surfaceElevated = SurfaceElevated,
        textMuted = TextMuted,
        divider = DividerColor,
        bgDark = BgDark
    )
}

private val DarkColorScheme = darkColorScheme(
    primary = EmeraldPrimary,
    onPrimary = BgDark,
    primaryContainer = EmeraldDark,
    secondary = CyanAccent,
    background = BgDark,
    surface = CardDark,
    surfaceVariant = SurfaceElevated,
    onBackground = TextPrimary,
    onSurface = TextPrimary,
    onSurfaceVariant = TextSecondary,
    outline = CardBorder,
    outlineVariant = CardBorder,
    error = RecordRed
)

private val LightColorScheme = lightColorScheme(
    primary = EmeraldPrimary,
    onPrimary = Color.White,
    primaryContainer = EmeraldDark,
    secondary = CyanAccent,
    background = JomiBg,
    surface = CardLight,
    surfaceVariant = SurfaceElevatedLight,
    onBackground = TextPrimaryLight,
    onSurface = TextPrimaryLight,
    onSurfaceVariant = TextSecondaryLight,
    outline = CardBorderLight,
    outlineVariant = CardBorderLight,
    error = RecordRed
)

private val DarkExtendedColors = ExtendedColors(
    cardBorder = CardBorder,
    surfaceElevated = SurfaceElevated,
    textMuted = TextMuted,
    divider = DividerColor,
    bgDark = BgDark
)

private val LightExtendedColors = ExtendedColors(
    cardBorder = CardBorderLight,
    surfaceElevated = SurfaceElevatedLight,
    textMuted = TextMutedLight,
    divider = DividerColorLight,
    bgDark = Color(0xFF1E293B)  // dark text for primary buttons in light mode
)

@Composable
fun ISFRecorderTheme(
    // Jomi v2 mockup is light-only; dark stays available for explicit callers.
    darkTheme: Boolean = false,
    content: @Composable () -> Unit
) {
    val colorScheme = if (darkTheme) DarkColorScheme else LightColorScheme
    val extendedColors = if (darkTheme) DarkExtendedColors else LightExtendedColors

    CompositionLocalProvider(LocalExtendedColors provides extendedColors) {
        MaterialTheme(
            colorScheme = colorScheme,
            content = content
        )
    }
}
