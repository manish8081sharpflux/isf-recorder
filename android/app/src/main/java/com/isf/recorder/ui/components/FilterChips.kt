package com.isf.recorder.ui.components

import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.isf.recorder.ui.theme.*

/**
 * Compact filter button for one line of filters ("Balgruh ▾" · "Coach ▾" · "Date ▾", 14 Oct):
 * shows [label] until something is chosen, then the choice (green). The menu has "All", the options
 * and a minimize row.
 */
@Composable
fun FilterChipMenu(
    label: String,
    value: String?,
    options: List<String>,
    onPick: (String?) -> Unit,
    modifier: Modifier = Modifier,
    allLabel: String = "All"
) {
    var open by remember { mutableStateOf(false) }
    Box(modifier) {
        FilterChipButton(text = value ?: label, active = value != null) { open = true }
        DropdownMenu(expanded = open, onDismissRequest = { open = false }, modifier = Modifier.heightIn(max = 360.dp)) {
            DropdownMenuItem(text = { Text(allLabel, fontWeight = if (value == null) FontWeight.Bold else FontWeight.Normal) },
                onClick = { open = false; onPick(null) })
            options.forEach { opt ->
                DropdownMenuItem(
                    text = { Text(opt, fontWeight = if (opt == value) FontWeight.Bold else FontWeight.Normal,
                        color = if (opt == value) JomiTeal else MaterialTheme.colorScheme.onBackground) },
                    onClick = { open = false; onPick(opt) }
                )
            }
            HorizontalDivider(color = JomiBorder)
            DropdownMenuItem(text = { Text("▲  Minimize list", color = JomiTeal, fontWeight = FontWeight.Bold) }, onClick = { open = false })
        }
    }
}

/** The Date filter as a compact button: All dates / Today / Yesterday / Custom range… */
@Composable
fun DateFilterChip(value: DateFilter, onChange: (DateFilter) -> Unit, modifier: Modifier = Modifier) {
    var open by remember { mutableStateOf(false) }
    var picker by remember { mutableStateOf(false) }
    val active = value != DateFilter.ALL
    Box(modifier) {
        FilterChipButton(text = if (active) value.label else "Date", active = active) { open = true }
        DropdownMenu(expanded = open, onDismissRequest = { open = false }) {
            listOf("all" to "All dates", "today" to "Today", "yesterday" to "Yesterday").forEach { (key, text) ->
                DropdownMenuItem(
                    text = { Text(text, fontWeight = if (value.key == key) FontWeight.Bold else FontWeight.Normal) },
                    onClick = { open = false; onChange(DateFilter.preset(key)) }
                )
            }
            DropdownMenuItem(text = { Text("Custom range…") }, onClick = { open = false; picker = true })
            HorizontalDivider(color = JomiBorder)
            DropdownMenuItem(text = { Text("▲  Minimize list", color = JomiTeal, fontWeight = FontWeight.Bold) }, onClick = { open = false })
        }
    }
    if (picker) DateRangeDialog(value, onDismiss = { picker = false }) { onChange(it); picker = false }
}

@Composable
private fun FilterChipButton(text: String, active: Boolean, onClick: () -> Unit) {
    Surface(
        onClick = onClick,
        shape = RoundedCornerShape(20.dp),
        color = if (active) JomiMint else Color.White,
        border = BorderStroke(1.dp, if (active) JomiGreen else JomiBorder),
        modifier = Modifier.fillMaxWidth()
    ) {
        Text(
            "$text ▾", color = if (active) JomiTeal else MaterialTheme.colorScheme.onBackground,
            fontSize = 13.sp, fontWeight = if (active) FontWeight.SemiBold else FontWeight.Normal,
            maxLines = 1, overflow = TextOverflow.Ellipsis, textAlign = TextAlign.Center,
            modifier = Modifier.padding(horizontal = 8.dp, vertical = 9.dp)
        )
    }
}
