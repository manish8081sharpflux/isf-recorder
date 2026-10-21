package com.isf.recorder.ui.components

import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.text.font.FontStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.isf.recorder.R
import com.isf.recorder.ui.theme.*

/** A person/name option. [userId] is null for list items and typed one-off names. */
data class NameOption(val name: String, val userId: String? = null)

private const val TYPE_A_NAME = "＋ Type a name…"

/** Field box shared by both dropdowns (mockup: white box, chevron on the right). */
@Composable
private fun DropdownField(
    text: String,
    placeholder: String,
    enabled: Boolean,
    onClick: () -> Unit
) {
    Surface(
        onClick = onClick,
        enabled = enabled,
        color = Color.White,
        shape = RoundedCornerShape(12.dp),
        border = BorderStroke(1.dp, JomiBorder),
        modifier = Modifier.fillMaxWidth().heightIn(min = 52.dp)
    ) {
        Row(modifier = Modifier.padding(horizontal = 14.dp, vertical = 12.dp), verticalAlignment = Alignment.CenterVertically) {
            Text(
                text = text.ifBlank { placeholder },
                color = when {
                    text.isBlank() -> JomiTextMuted
                    !enabled -> MaterialTheme.colorScheme.onBackground.copy(alpha = 0.6f)
                    else -> MaterialTheme.colorScheme.onBackground
                },
                fontSize = 15.sp,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis,
                modifier = Modifier.weight(1f)
            )
            Icon(painterResource(R.drawable.ic_expand_more), null, tint = JomiTextMuted.copy(alpha = if (enabled) 1f else 0.4f), modifier = Modifier.size(22.dp))
        }
    }
}

@Composable
private fun TypeNameDialog(title: String, onDismiss: () -> Unit, onDone: (String) -> Unit) {
    var value by remember { mutableStateOf("") }
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text(title, fontWeight = FontWeight.Bold) },
        text = {
            OutlinedTextField(
                value = value,
                onValueChange = { value = it },
                singleLine = true,
                placeholder = { Text("Name") },
                modifier = Modifier.fillMaxWidth()
            )
        },
        confirmButton = {
            TextButton(onClick = { onDone(value.trim()) }, enabled = value.isNotBlank()) { Text("Use name") }
        },
        dismissButton = { TextButton(onClick = onDismiss) { Text("Cancel") } }
    )
}

/**
 * Label above + dropdown. With [allowCustom], the last menu entry is "Type a name…" for a one-off name.
 */
@Composable
fun SelectDropdown(
    label: String,
    options: List<NameOption>,
    selected: NameOption?,
    onSelect: (NameOption) -> Unit,
    modifier: Modifier = Modifier,
    placeholder: String = "Select",
    enabled: Boolean = true,
    allowCustom: Boolean = false
) {
    var expanded by remember { mutableStateOf(false) }
    var typing by remember { mutableStateOf(false) }
    Column(modifier = modifier) {
        Text(label, color = MaterialTheme.colorScheme.onBackground, fontSize = 13.sp, fontWeight = FontWeight.Medium)
        Spacer(Modifier.height(6.dp))
        Box {
            DropdownField(selected?.name.orEmpty(), placeholder, enabled) { expanded = true }
            DropdownMenu(expanded = expanded, onDismissRequest = { expanded = false }, modifier = Modifier.heightIn(max = 360.dp).widthIn(min = 220.dp, max = 340.dp)) {
                if (options.isEmpty() && !allowCustom) {
                    DropdownMenuItem(text = { Text("No names yet — ask your admin", color = JomiTextMuted) }, onClick = { expanded = false }, enabled = false)
                }
                options.forEach { opt ->
                    DropdownMenuItem(
                        text = {
                            Text(
                                opt.name,
                                fontWeight = if (opt == selected) FontWeight.Bold else FontWeight.Normal,
                                color = if (opt == selected) JomiTeal else MaterialTheme.colorScheme.onBackground
                            )
                        },
                        onClick = {
                            expanded = false
                            onSelect(opt)
                        }
                    )
                }
                if (allowCustom) {
                    if (options.isNotEmpty()) HorizontalDivider(color = JomiBorder)
                    DropdownMenuItem(
                        text = { Text(TYPE_A_NAME, color = JomiTeal, fontStyle = FontStyle.Italic) },
                        onClick = {
                            expanded = false
                            typing = true
                        }
                    )
                }
                MinimizeListItem { expanded = false }
            }
        }
    }
    if (typing) {
        TypeNameDialog(label, onDismiss = { typing = false }) { name ->
            typing = false
            if (name.isNotBlank()) onSelect(NameOption(name, null))
        }
    }
}

/**
 * Multi-select (meeting participants): checkable names + "Type a name…" one-off entries.
 */
@OptIn(ExperimentalLayoutApi::class)
@Composable
fun MultiSelectDropdown(
    label: String,
    options: List<NameOption>,
    selected: List<NameOption>,
    onChange: (List<NameOption>) -> Unit,
    modifier: Modifier = Modifier,
    placeholder: String = "Optional",
    enabled: Boolean = true,
    allowCustom: Boolean = true
) {
    var expanded by remember { mutableStateOf(false) }
    var typing by remember { mutableStateOf(false) }
    val summary = when {
        selected.isEmpty() -> ""
        selected.size == 1 -> "1 selected"
        else -> "${selected.size} selected"
    }
    // Typed one-off names are shown in the menu too so they can be unticked.
    val allOptions = options + selected.filter { s -> options.none { it.name.equals(s.name, true) } }
    Column(modifier = modifier) {
        Text(label, color = MaterialTheme.colorScheme.onBackground, fontSize = 13.sp, fontWeight = FontWeight.Medium)
        Spacer(Modifier.height(6.dp))
        Box {
            DropdownField(summary, placeholder, enabled) { expanded = true }
            DropdownMenu(expanded = expanded, onDismissRequest = { expanded = false }, modifier = Modifier.heightIn(max = 380.dp).widthIn(min = 220.dp, max = 340.dp)) {
                allOptions.forEach { opt ->
                    val checked = selected.any { it.name.equals(opt.name, true) }
                    DropdownMenuItem(
                        leadingIcon = {
                            Checkbox(checked = checked, onCheckedChange = null, colors = CheckboxDefaults.colors(checkedColor = JomiGreen))
                        },
                        text = { Text(opt.name) },
                        onClick = {
                            onChange(
                                if (checked) selected.filterNot { it.name.equals(opt.name, true) } else selected + opt
                            )
                        }
                    )
                }
                if (allowCustom) {
                    if (allOptions.isNotEmpty()) HorizontalDivider(color = JomiBorder)
                    DropdownMenuItem(
                        text = { Text(TYPE_A_NAME, color = JomiTeal, fontStyle = FontStyle.Italic) },
                        onClick = {
                            expanded = false
                            typing = true
                        }
                    )
                }
                MinimizeListItem("▲  Done — minimize list") { expanded = false }
            }
        }
        if (selected.isNotEmpty()) {
            Spacer(Modifier.height(6.dp))
            FlowRow(horizontalArrangement = Arrangement.spacedBy(6.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
                selected.forEach { opt ->
                    InputChip(
                        selected = true,
                        enabled = enabled,
                        onClick = { if (enabled) onChange(selected.filterNot { it.name.equals(opt.name, true) }) },
                        label = { Text(opt.name, maxLines = 1, overflow = TextOverflow.Ellipsis, modifier = Modifier.widthIn(max = 220.dp)) },
                        trailingIcon = if (enabled) {
                            { Icon(painterResource(R.drawable.ic_close), "Remove ${opt.name}", modifier = Modifier.size(16.dp)) }
                        } else null,
                        colors = InputChipDefaults.inputChipColors(selectedContainerColor = JomiMint, selectedLabelColor = JomiTeal)
                    )
                }
            }
        }
    }
    if (typing) {
        TypeNameDialog("Add participant", onDismiss = { typing = false }) { name ->
            typing = false
            if (name.isNotBlank() && selected.none { it.name.equals(name, true) }) onChange(selected + NameOption(name, null))
        }
    }
}

/** Last row of every list: closes it so the main screen is reachable without scrolling back up. */
@Composable
private fun MinimizeListItem(label: String = "▲  Minimize list", onClick: () -> Unit) {
    HorizontalDivider(color = JomiBorder)
    DropdownMenuItem(
        text = { Text(label, color = JomiTeal, fontWeight = FontWeight.Bold, modifier = Modifier.fillMaxWidth(),
            textAlign = androidx.compose.ui.text.style.TextAlign.Center) },
        onClick = onClick
    )
}
