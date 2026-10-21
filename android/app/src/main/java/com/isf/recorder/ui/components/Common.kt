package com.isf.recorder.ui.components

import androidx.annotation.DrawableRes
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.*
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.isf.recorder.R
import com.isf.recorder.ui.theme.*

/** White rounded card with a hairline border (mockup card). */
@Composable
fun JomiCard(
    modifier: Modifier = Modifier,
    padding: Dp = 16.dp,
    color: Color = JomiCard,
    border: Color = JomiBorder,
    content: @Composable ColumnScope.() -> Unit
) {
    Surface(
        color = color,
        shape = RoundedCornerShape(18.dp),
        border = BorderStroke(1.dp, border),
        modifier = modifier.fillMaxWidth()
    ) {
        Column(modifier = Modifier.padding(padding), content = content)
    }
}

/** Small outlined icon+label button ("Report", "AI Chat", "Copy", "Share"). */
@Composable
fun ActionChipButton(
    label: String,
    @DrawableRes icon: Int?,
    onClick: () -> Unit,
    modifier: Modifier = Modifier,
    enabled: Boolean = true,
    tint: Color = JomiTeal,
    container: Color = JomiMint.copy(alpha = 0.55f),
    border: Color = JomiMintBorder
) {
    OutlinedButton(
        onClick = onClick,
        enabled = enabled,
        shape = RoundedCornerShape(10.dp),
        border = BorderStroke(1.dp, if (enabled) border else JomiBorder),
        colors = ButtonDefaults.outlinedButtonColors(
            containerColor = if (enabled) container else Color.Transparent,
            contentColor = tint,
            disabledContentColor = JomiTextMuted.copy(alpha = 0.6f)
        ),
        contentPadding = PaddingValues(horizontal = 10.dp, vertical = 6.dp),
        modifier = modifier.heightIn(min = 36.dp)
    ) {
        if (icon != null) {
            Icon(painterResource(icon), contentDescription = null, modifier = Modifier.size(16.dp))
            Spacer(Modifier.width(6.dp))
        }
        Text(label, fontSize = 13.sp, fontWeight = FontWeight.Medium, maxLines = 1, overflow = TextOverflow.Ellipsis)
    }
}

/** Plain tinted icon button. */
@Composable
fun SmallIconButton(
    @DrawableRes icon: Int,
    contentDescription: String,
    onClick: () -> Unit,
    modifier: Modifier = Modifier,
    tint: Color = JomiTextMuted,
    background: Color = Color.Transparent,
    enabled: Boolean = true,
    size: Dp = 36.dp
) {
    Surface(
        onClick = onClick,
        enabled = enabled,
        color = background,
        shape = RoundedCornerShape(10.dp),
        modifier = modifier.size(size)
    ) {
        Box(contentAlignment = Alignment.Center) {
            Icon(
                painterResource(icon),
                contentDescription = contentDescription,
                tint = if (enabled) tint else tint.copy(alpha = 0.35f),
                modifier = Modifier.size(size * 0.5f)
            )
        }
    }
}

/** "Completed" / "Transcribing" / "Error" pill. */
@Composable
fun StatusPill(text: String, color: Color, modifier: Modifier = Modifier) {
    Surface(color = color.copy(alpha = 0.12f), shape = RoundedCornerShape(20.dp), modifier = modifier) {
        Text(
            text = text,
            color = color,
            fontSize = 11.sp,
            fontWeight = FontWeight.SemiBold,
            maxLines = 1,
            modifier = Modifier.padding(horizontal = 8.dp, vertical = 3.dp)
        )
    }
}

/** Status text + colour for a recording / report pair (legacy status values only, §4.4). */
fun statusLabel(status: String?, reportStatus: String?): Pair<String, Color> = when (status) {
    "recording" -> "Recording" to RecordRed
    "uploading" -> "Uploading" to WarningOrange
    "processing", "stitching", "pending" -> "Transcribing" to CyanAccent
    "error" -> "Error" to RecordRed
    "completed" -> when (reportStatus) {
        "generating" -> "Writing report" to CyanAccent
        "final" -> "Final" to JomiTeal
        "draft" -> "Report ready" to JomiGreen
        "error" -> "Report failed" to WarningOrange
        else -> "Completed" to JomiGreen
    }
    else -> (status?.replaceFirstChar { it.uppercase() } ?: "—") to JomiTextMuted
}

/** Screen title row with a back arrow. */
@Composable
fun BackHeader(
    title: String,
    onBack: () -> Unit,
    modifier: Modifier = Modifier,
    subtitle: String? = null,
    actions: @Composable RowScope.() -> Unit = {}
) {
    Row(modifier = modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
        SmallIconButton(R.drawable.ic_arrow_back, "Back", onBack, tint = MaterialTheme.colorScheme.onBackground, size = 40.dp)
        Spacer(Modifier.width(6.dp))
        Column(modifier = Modifier.weight(1f)) {
            Text(
                title,
                color = MaterialTheme.colorScheme.onBackground,
                fontSize = 19.sp,
                fontWeight = FontWeight.Bold,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis
            )
            if (!subtitle.isNullOrBlank()) {
                Text(subtitle, color = JomiTextMuted, fontSize = 12.sp, maxLines = 1, overflow = TextOverflow.Ellipsis)
            }
        }
        actions()
    }
}

/** Inline error/warning banner. */
@Composable
fun MessageBanner(text: String, color: Color = WarningOrange, modifier: Modifier = Modifier, onDismiss: (() -> Unit)? = null) {
    Surface(
        color = color.copy(alpha = 0.10f),
        shape = RoundedCornerShape(12.dp),
        border = BorderStroke(1.dp, color.copy(alpha = 0.35f)),
        modifier = modifier.fillMaxWidth()
    ) {
        Row(modifier = Modifier.padding(horizontal = 12.dp, vertical = 10.dp), verticalAlignment = Alignment.CenterVertically) {
            Icon(painterResource(R.drawable.ic_warning), contentDescription = null, tint = color, modifier = Modifier.size(18.dp))
            Spacer(Modifier.width(8.dp))
            Text(text, color = MaterialTheme.colorScheme.onBackground, fontSize = 13.sp, modifier = Modifier.weight(1f))
            if (onDismiss != null) {
                SmallIconButton(R.drawable.ic_close, "Dismiss", onDismiss, size = 28.dp)
            }
        }
    }
}
