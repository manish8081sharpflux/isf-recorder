package com.isf.recorder.ui.components

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.tooling.preview.Preview
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.isf.recorder.ui.theme.*

/**
 * "Meeting | Schedule" segmented control. [modes] are API values ("meeting", "schedule");
 * disabled (locked) while a recording session exists.
 */
@Composable
fun ModeToggle(
    modes: List<String>,
    selected: String,
    onSelect: (String) -> Unit,
    modifier: Modifier = Modifier,
    enabled: Boolean = true,
    labels: Map<String, String> = emptyMap()
) {
    Surface(color = JomiSegmentBg, shape = RoundedCornerShape(14.dp), modifier = modifier.fillMaxWidth()) {
        Row(modifier = Modifier.padding(4.dp)) {
            modes.forEach { mode ->
                val isSel = mode == selected
                Surface(
                    onClick = { if (enabled && !isSel) onSelect(mode) },
                    enabled = enabled || isSel,
                    color = if (isSel) Color.White else Color.Transparent,
                    shadowElevation = if (isSel) 1.dp else 0.dp,
                    shape = RoundedCornerShape(11.dp),
                    modifier = Modifier.weight(1f).height(44.dp)
                ) {
                    Box(contentAlignment = Alignment.Center) {
                        Text(
                            text = labels[mode] ?: modeLabel(mode) ?: mode.replaceFirstChar { it.uppercase() },
                            color = when {
                                isSel -> JomiTeal
                                !enabled -> JomiTextMuted.copy(alpha = 0.5f)
                                else -> JomiTextMuted
                            },
                            fontSize = 15.sp,
                            fontWeight = if (isSel) FontWeight.Bold else FontWeight.Medium
                        )
                    }
                }
            }
        }
    }
}

@Preview(showBackground = true, widthDp = 360)
@Composable
private fun ModeTogglePreview() {
    ISFRecorderTheme {
        Column(Modifier.padding(16.dp).background(MaterialTheme.colorScheme.background)) {
            ModeToggle(listOf("meeting", "schedule"), "schedule", {})
        }
    }
}
