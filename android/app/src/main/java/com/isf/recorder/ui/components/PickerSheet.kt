package com.isf.recorder.ui.components

import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.text.font.FontStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.isf.recorder.R
import com.isf.recorder.ui.theme.*

/**
 * Home recorder pickers (21 Oct redesign): a compact field that opens a bottom sheet with a title, ✕, search,
 * one row per name with a coloured circle, a second line (Balgruha area / "Assistant coach"), and a radio
 * (single) or checkbox (multi). "Type a name…" stays available for one-off names.
 */

/** "Vikas Anath Ashram, Chikhali" → ("Vikas Anath Ashram", "Chikhali"); no comma → (name, null). */
fun splitNameArea(full: String): Pair<String, String?> {
    val i = full.lastIndexOf(',')
    if (i <= 0 || i >= full.length - 1) return full.trim() to null
    val area = full.substring(i + 1).trim()
    return full.substring(0, i).trim() to area.ifBlank { null }
}

private val AVATAR_COLORS = listOf(
    Color(0xFFFCE7F3) to Color(0xFFDB2777),   // pink
    Color(0xFFDBEAFE) to Color(0xFF2563EB),   // blue
    Color(0xFFEDE9FE) to Color(0xFF7C3AED),   // purple
    Color(0xFFFFEDD5) to Color(0xFFEA580C),   // orange
    Color(0xFFDCFCE7) to Color(0xFF16A34A),   // green
    Color(0xFFE0F2FE) to Color(0xFF0891B2),   // cyan
)

/** Stable colour pair (background, foreground) for a name. */
fun avatarColors(name: String): Pair<Color, Color> =
    AVATAR_COLORS[(name.lowercase().hashCode() and 0x7fffffff) % AVATAR_COLORS.size]

enum class PickerAvatar { PEOPLE_ICON, INITIALS }

/** Label + compact field (white box, chevron) that opens the sheet. */
@Composable
fun PickerField(label: String, text: String, placeholder: String, enabled: Boolean, onClick: () -> Unit, modifier: Modifier = Modifier) {
    Column(modifier) {
        Text(label, color = MaterialTheme.colorScheme.onBackground, fontSize = 13.sp, fontWeight = FontWeight.SemiBold)
        Spacer(Modifier.height(6.dp))
        Surface(
            onClick = onClick, enabled = enabled, color = Color.White, shape = RoundedCornerShape(12.dp),
            border = BorderStroke(1.dp, JomiBorder), modifier = Modifier.fillMaxWidth().heightIn(min = 48.dp)
        ) {
            Row(Modifier.padding(horizontal = 12.dp, vertical = 12.dp), verticalAlignment = Alignment.CenterVertically) {
                Text(
                    text.ifBlank { placeholder },
                    color = when {
                        text.isBlank() -> JomiTextMuted
                        !enabled -> MaterialTheme.colorScheme.onBackground.copy(alpha = 0.6f)
                        else -> MaterialTheme.colorScheme.onBackground
                    },
                    fontSize = 14.sp, maxLines = 1, overflow = TextOverflow.Ellipsis, modifier = Modifier.weight(1f)
                )
                Icon(painterResource(R.drawable.ic_expand_more), null,
                    tint = JomiTextMuted.copy(alpha = if (enabled) 1f else 0.4f), modifier = Modifier.size(20.dp))
            }
        }
    }
}

@Composable
private fun Avatar(name: String, style: PickerAvatar) {
    val (bg, fg) = avatarColors(name)
    Box(Modifier.size(42.dp).clip(CircleShape).background(bg), contentAlignment = Alignment.Center) {
        if (style == PickerAvatar.PEOPLE_ICON) Icon(painterResource(R.drawable.ic_people), null, tint = fg, modifier = Modifier.size(22.dp))
        else Text(initialsOf(name), color = fg, fontWeight = FontWeight.Bold, fontSize = 14.sp)
    }
}

/** The sheet itself. [isSelected] decides radio / checkbox state; [onPick] is called per tapped row. */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun PickerSheet(
    title: String,
    searchHint: String,
    options: List<NameOption>,
    multi: Boolean,
    isSelected: (NameOption) -> Boolean,
    onPick: (NameOption) -> Unit,
    onDismiss: () -> Unit,
    avatar: PickerAvatar,
    subtitle: (NameOption) -> String?,
    showAreaSplit: Boolean = false,
    allowCustom: Boolean = false,
    onTypeName: (() -> Unit)? = null
) {
    val sheetState = rememberModalBottomSheetState(skipPartiallyExpanded = true)
    var query by remember { mutableStateOf("") }
    val shown = remember(options, query) {
        val q = query.trim()
        if (q.isEmpty()) options else options.filter { it.name.contains(q, ignoreCase = true) }
    }
    ModalBottomSheet(onDismissRequest = onDismiss, sheetState = sheetState, containerColor = Color.White) {
        Column(Modifier.fillMaxWidth().padding(horizontal = 16.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(title, fontWeight = FontWeight.Bold, fontSize = 18.sp, modifier = Modifier.weight(1f))
                IconButton(onClick = onDismiss) { Icon(painterResource(R.drawable.ic_close), "Close", tint = JomiTextMuted) }
            }
            OutlinedTextField(
                value = query, onValueChange = { query = it }, singleLine = true,
                placeholder = { Text(searchHint, color = JomiTextMuted) },
                leadingIcon = { Text("🔍", fontSize = 16.sp) },
                shape = RoundedCornerShape(12.dp),
                colors = OutlinedTextFieldDefaults.colors(
                    focusedContainerColor = JomiBg, unfocusedContainerColor = JomiBg,
                    focusedBorderColor = JomiGreen, unfocusedBorderColor = Color.Transparent
                ),
                modifier = Modifier.fillMaxWidth()
            )
            Spacer(Modifier.height(10.dp))
            LazyColumn(Modifier.heightIn(max = 460.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                if (shown.isEmpty()) item {
                    Text(if (options.isEmpty()) "No names yet — ask your admin" else "No match for \"$query\"",
                        color = JomiTextMuted, fontSize = 14.sp, modifier = Modifier.padding(vertical = 12.dp))
                }
                items(shown, key = { it.name }) { opt ->
                    val sel = isSelected(opt)
                    val (main, area) = if (showAreaSplit) splitNameArea(opt.name) else opt.name to null
                    Surface(
                        onClick = { onPick(opt) },
                        color = if (sel) JomiMint else Color.White,
                        shape = RoundedCornerShape(14.dp),
                        border = BorderStroke(1.dp, if (sel) JomiMintBorder else JomiBorder),
                        modifier = Modifier.fillMaxWidth()
                    ) {
                        Row(Modifier.padding(horizontal = 12.dp, vertical = 10.dp), verticalAlignment = Alignment.CenterVertically) {
                            Avatar(main, avatar)
                            Spacer(Modifier.width(12.dp))
                            Column(Modifier.weight(1f)) {
                                Text(main, fontWeight = FontWeight.SemiBold, fontSize = 15.sp, maxLines = 2, overflow = TextOverflow.Ellipsis)
                                (area ?: subtitle(opt))?.let { Text(it, color = JomiTextMuted, fontSize = 12.sp, maxLines = 1, overflow = TextOverflow.Ellipsis) }
                            }
                            if (multi) Checkbox(checked = sel, onCheckedChange = { onPick(opt) },
                                colors = CheckboxDefaults.colors(checkedColor = JomiGreen))
                            else RadioButton(selected = sel, onClick = { onPick(opt) },
                                colors = RadioButtonDefaults.colors(selectedColor = JomiGreen))
                        }
                    }
                }
                if (allowCustom && onTypeName != null) item {
                    Text("＋ Type a name…", color = JomiTeal, fontStyle = FontStyle.Italic, fontSize = 15.sp,
                        modifier = Modifier.fillMaxWidth().clickable { onTypeName() }.padding(vertical = 12.dp, horizontal = 4.dp))
                }
            }
            if (multi) {
                Spacer(Modifier.height(10.dp))
                Button(onClick = onDismiss, colors = ButtonDefaults.buttonColors(containerColor = JomiGreen), modifier = Modifier.fillMaxWidth()) {
                    Text("Done")
                }
            }
            Spacer(Modifier.height(24.dp))
        }
    }
}

@Composable
private fun TypeOneName(title: String, onDismiss: () -> Unit, onDone: (String) -> Unit) {
    var value by remember { mutableStateOf("") }
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text(title, fontWeight = FontWeight.Bold) },
        text = { OutlinedTextField(value = value, onValueChange = { value = it }, singleLine = true, placeholder = { Text("Name") }) },
        confirmButton = { TextButton(onClick = { onDone(value.trim()) }, enabled = value.isNotBlank()) { Text("Use name") } },
        dismissButton = { TextButton(onClick = onDismiss) { Text("Cancel") } }
    )
}

/** One choice (Balgruha): field + sheet. Picking a row closes the sheet. */
@Composable
fun SinglePicker(
    label: String,
    sheetTitle: String,
    searchHint: String,
    options: List<NameOption>,
    selected: NameOption?,
    onSelect: (NameOption) -> Unit,
    modifier: Modifier = Modifier,
    enabled: Boolean = true,
    allowCustom: Boolean = false,
    avatar: PickerAvatar = PickerAvatar.PEOPLE_ICON,
    subtitle: (NameOption) -> String? = { null },
    showAreaSplit: Boolean = true
) {
    var open by remember { mutableStateOf(false) }
    var typing by remember { mutableStateOf(false) }
    val shownText = selected?.name?.let { if (showAreaSplit) splitNameArea(it).first else it }.orEmpty()
    PickerField(label, shownText, "Select", enabled, { open = true }, modifier)
    if (open) PickerSheet(
        title = sheetTitle, searchHint = searchHint, options = options, multi = false,
        isSelected = { it.name == selected?.name }, onPick = { onSelect(it); open = false }, onDismiss = { open = false },
        avatar = avatar, subtitle = subtitle, showAreaSplit = showAreaSplit,
        allowCustom = allowCustom, onTypeName = { open = false; typing = true }
    )
    if (typing) TypeOneName(label, onDismiss = { typing = false }) { name ->
        typing = false
        if (name.isNotBlank()) onSelect(NameOption(name, null))
    }
}

/** Several choices (assistant coaches / participants): field + sheet with checkboxes and Done. */
@Composable
fun MultiPicker(
    label: String,
    sheetTitle: String,
    searchHint: String,
    options: List<NameOption>,
    selected: List<NameOption>,
    onChange: (List<NameOption>) -> Unit,
    modifier: Modifier = Modifier,
    enabled: Boolean = true,
    allowCustom: Boolean = true,
    subtitle: (NameOption) -> String? = { null }
) {
    var open by remember { mutableStateOf(false) }
    var typing by remember { mutableStateOf(false) }
    // typed one-off names are listed too, so they can be unticked
    val all = options + selected.filter { s -> options.none { it.name.equals(s.name, true) } }
    PickerField(label, selected.joinToString(", ") { it.name }, "Choose one or more", enabled, { open = true }, modifier)
    if (open) PickerSheet(
        title = sheetTitle, searchHint = searchHint, options = all, multi = true,
        isSelected = { o -> selected.any { it.name.equals(o.name, true) } },
        onPick = { o ->
            onChange(if (selected.any { it.name.equals(o.name, true) }) selected.filterNot { it.name.equals(o.name, true) } else selected + o)
        },
        onDismiss = { open = false }, avatar = PickerAvatar.INITIALS, subtitle = subtitle,
        allowCustom = allowCustom, onTypeName = { open = false; typing = true }
    )
    if (typing) TypeOneName(label, onDismiss = { typing = false }) { name ->
        typing = false
        if (name.isNotBlank() && selected.none { it.name.equals(name, true) }) onChange(selected + NameOption(name, null))
    }
}
