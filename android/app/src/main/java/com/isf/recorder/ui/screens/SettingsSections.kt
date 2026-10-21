package com.isf.recorder.ui.screens

import android.widget.Toast
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.style.TextDecoration
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.isf.recorder.R
import com.isf.recorder.data.api.ISFApiClient
import com.isf.recorder.data.prefs.UserPreferences
import com.isf.recorder.ui.components.*
import com.isf.recorder.ui.theme.*
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch

@Composable
private fun SectionTitle(title: String, subtitle: String? = null, action: @Composable RowScope.() -> Unit = {}) {
    Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.fillMaxWidth()) {
        Column(Modifier.weight(1f)) {
            Text(title.uppercase(), color = JomiTeal, fontSize = 11.sp, fontWeight = FontWeight.Bold, letterSpacing = 1.sp)
            if (subtitle != null) Text(subtitle, color = JomiTextMuted, fontSize = 12.sp)
        }
        action()
    }
}

private fun isArchived(u: Map<String, Any?>): Boolean =
    u["is_archived"] == true || (u["archived_at"] != null && u["archived_at"].toString() != "null")

// ═════════════════════════════════════════════════════════════════════════════
// Team (contract §2): add with live duplicate warning, edit, archive / delete
// ═════════════════════════════════════════════════════════════════════════════

@Composable
fun TeamSection(api: ISFApiClient, prefs: UserPreferences) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    val roleLabel = prefs.roleLabel
    var members by remember { mutableStateOf<List<Map<String, Any?>>>(emptyList()) }
    var loading by remember { mutableStateOf(true) }
    var showArchived by remember { mutableStateOf(false) }
    var search by remember { mutableStateOf("") }
    var showAdd by remember { mutableStateOf(false) }
    var editTarget by remember { mutableStateOf<Map<String, Any?>?>(null) }
    var removeTarget by remember { mutableStateOf<Map<String, Any?>?>(null) }
    var permanentTarget by remember { mutableStateOf<Map<String, Any?>?>(null) }
    var busy by remember { mutableStateOf(false) }

    fun load() {
        scope.launch {
            val res = api.listUsers(includeArchived = true)
            loading = false
            res.onSuccess { members = it }
                .onFailure { Toast.makeText(context, it.message ?: "Could not load team", Toast.LENGTH_SHORT).show() }
        }
    }
    LaunchedEffect(Unit) { load() }

    fun runAction(block: suspend () -> Result<*>, ok: String) {
        busy = true
        scope.launch {
            val res = block()
            busy = false
            if (res.isSuccess) {
                Toast.makeText(context, ok, Toast.LENGTH_SHORT).show()
                load()
            } else {
                Toast.makeText(context, res.exceptionOrNull()?.message ?: "Failed", Toast.LENGTH_LONG).show()
            }
        }
    }

    JomiCard {
        SectionTitle("Team", "People who can sign in and record") {
            Button(
                onClick = { showAdd = true },
                colors = ButtonDefaults.buttonColors(containerColor = JomiGreen),
                shape = RoundedCornerShape(10.dp),
                contentPadding = PaddingValues(horizontal = 12.dp, vertical = 6.dp)
            ) { Text("Add person", fontSize = 13.sp) }
        }
        Spacer(Modifier.height(10.dp))
        OutlinedTextField(
            value = search, onValueChange = { search = it }, singleLine = true,
            placeholder = { Text("Search name or number") },
            colors = OutlinedTextFieldDefaults.colors(focusedBorderColor = JomiGreen, unfocusedBorderColor = JomiBorder),
            shape = RoundedCornerShape(12.dp), modifier = Modifier.fillMaxWidth()
        )
        Spacer(Modifier.height(8.dp))
        val archivedCount = members.count(::isArchived)
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            FilterChip(
                selected = !showArchived, onClick = { showArchived = false },
                label = { Text("Active (${members.size - archivedCount})") },
                colors = FilterChipDefaults.filterChipColors(selectedContainerColor = JomiMint, selectedLabelColor = JomiTeal)
            )
            FilterChip(
                selected = showArchived, onClick = { showArchived = true },
                label = { Text("Archived ($archivedCount)") },
                colors = FilterChipDefaults.filterChipColors(selectedContainerColor = JomiMint, selectedLabelColor = JomiTeal)
            )
        }
        Spacer(Modifier.height(8.dp))
        val q = search.trim()
        val qDigits = q.filter { it.isDigit() }
        val shown = members.filter { isArchived(it) == showArchived }.filter { m ->
            q.isEmpty() || (m["name"]?.toString() ?: "").contains(q, ignoreCase = true) ||
                (qDigits.isNotEmpty() && (m["phone"]?.toString() ?: "").filter { it.isDigit() }.contains(qDigits))
        }
        when {
            loading -> CircularProgressIndicator(Modifier.size(22.dp).align(Alignment.CenterHorizontally), color = JomiGreen, strokeWidth = 2.dp)
            shown.isEmpty() -> Text(if (showArchived) "Nobody is archived." else "No team members yet.", color = JomiTextMuted, fontSize = 13.sp)
            else -> Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                shown.forEach { m ->
                    val id = m["id"]?.toString().orEmpty()
                    val name = m["name"]?.toString()?.ifBlank { null } ?: "Unnamed"
                    val isAdminUser = m["role"] == "admin"
                    val isSuper = m["is_super_admin"] == true
                    val isSelf = id == prefs.userId
                    Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.fillMaxWidth()) {
                        Box(
                            Modifier.size(36.dp).background(if (isAdminUser) JomiMint else JomiSegmentBg, CircleShape),
                            contentAlignment = Alignment.Center
                        ) { Text(initialsOf(name), color = JomiTeal, fontWeight = FontWeight.Bold, fontSize = 13.sp) }
                        Spacer(Modifier.width(10.dp))
                        Column(Modifier.weight(1f)) {
                            Row(verticalAlignment = Alignment.CenterVertically) {
                                Text(name, fontWeight = FontWeight.SemiBold, fontSize = 14.sp, maxLines = 1, overflow = TextOverflow.Ellipsis,
                                    modifier = Modifier.weight(1f, fill = false))
                                Spacer(Modifier.width(6.dp))
                                StatusPill(if (isSuper) "Super admin" else if (isAdminUser) "Admin" else roleLabel, if (isAdminUser) JomiTeal else JomiTextMuted)
                            }
                            Text(m["phone"]?.toString() ?: "", color = JomiTextMuted, fontSize = 12.sp, maxLines = 1, overflow = TextOverflow.Ellipsis)
                        }
                        if (showArchived) {
                            TextButton(onClick = { runAction({ api.unarchiveUser(id) }, "$name can sign in again") }, enabled = !busy) {
                                Text("Un-archive", color = JomiTeal)
                            }
                        } else {
                            SmallIconButton(R.drawable.ic_edit, "Edit $name", { editTarget = m }, tint = JomiGreen)
                            if (!isSelf && !isSuper) {
                                SmallIconButton(R.drawable.ic_delete, "Remove $name", { removeTarget = m })
                            }
                        }
                    }
                }
            }
        }
    }

    if (showAdd) {
        PersonDialog(
            api = api, prefs = prefs, existing = null,
            onDismiss = { showAdd = false },
            onSaved = { showAdd = false; load() },
            onUnarchive = { id, name -> showAdd = false; runAction({ api.unarchiveUser(id) }, "$name can sign in again") }
        )
    }
    editTarget?.let { m ->
        PersonDialog(
            api = api, prefs = prefs, existing = m,
            onDismiss = { editTarget = null },
            onSaved = { editTarget = null; load() },
            onUnarchive = { _, _ -> }
        )
    }
    removeTarget?.let { m ->
        val name = m["name"]?.toString() ?: "this person"
        val id = m["id"].toString()
        AlertDialog(
            onDismissRequest = { removeTarget = null },
            title = { Text("Remove $name?", fontWeight = FontWeight.Bold) },
            text = {
                Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
                    RemoveOption(
                        title = "Archive (recommended)",
                        body = "$name can no longer sign in and is hidden from lists. All recordings are kept. You can un-archive later.",
                        color = JomiTeal
                    ) {
                        removeTarget = null
                        runAction({ api.archiveUser(id) }, "$name archived")
                    }
                    RemoveOption(
                        title = "Permanently delete",
                        body = "The account is removed for good. Recordings stay, labelled with $name's name. Their AI chats are deleted. This cannot be undone.",
                        color = RecordRed
                    ) {
                        removeTarget = null
                        permanentTarget = m
                    }
                }
            },
            confirmButton = {},
            dismissButton = { TextButton(onClick = { removeTarget = null }) { Text("Cancel") } }
        )
    }
    permanentTarget?.let { m ->
        val name = m["name"]?.toString() ?: "this person"
        StrongDeleteDialog(
            itemTitle = name,
            title = "Permanently delete this person?",
            whatIsDeleted = listOf("their account and sign-in", "their AI chat history"),
            confirmLabel = "Delete forever",
            busy = busy,
            onDismiss = { permanentTarget = null },
            onConfirm = {
                busy = true
                scope.launch {
                    val res = api.deleteUserPermanently(m["id"].toString())
                    busy = false
                    if (res.isSuccess) {
                        permanentTarget = null
                        val kept = (res.getOrNull()?.get("recordings_kept") as? Number)?.toInt()
                        Toast.makeText(context, "$name deleted" + (kept?.let { " · $it recordings kept" } ?: ""), Toast.LENGTH_LONG).show()
                        load()
                    } else {
                        Toast.makeText(context, res.exceptionOrNull()?.message ?: "Could not delete", Toast.LENGTH_LONG).show()
                    }
                }
            }
        )
    }
}

@Composable
private fun RemoveOption(title: String, body: String, color: Color, onClick: () -> Unit) {
    Surface(
        onClick = onClick,
        shape = RoundedCornerShape(12.dp),
        border = BorderStroke(1.dp, color.copy(alpha = 0.4f)),
        color = color.copy(alpha = 0.05f),
        modifier = Modifier.fillMaxWidth()
    ) {
        Column(Modifier.padding(12.dp)) {
            Text(title, color = color, fontWeight = FontWeight.Bold, fontSize = 14.sp)
            Spacer(Modifier.height(2.dp))
            Text(body, fontSize = 12.sp, color = MaterialTheme.colorScheme.onBackground)
        }
    }
}

/**
 * Add (existing == null) or edit a person. Role chips only for super admins (contract §2.6/§2.7).
 * Add shows a live duplicate warning from GET /users/check.
 */
@Composable
private fun PersonDialog(
    api: ISFApiClient,
    prefs: UserPreferences,
    existing: Map<String, Any?>?,
    onDismiss: () -> Unit,
    onSaved: () -> Unit,
    onUnarchive: (id: String, name: String) -> Unit
) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    val isEdit = existing != null
    val origName = existing?.get("name")?.toString().orEmpty()
    val origPhone = existing?.get("phone")?.toString().orEmpty()
    val origRole = existing?.get("role")?.toString() ?: "staff"
    val targetIsSuper = existing?.get("is_super_admin") == true
    // Non-admins get masked phones; only allow phone edits when we see the real number.
    val phoneEditable = !origPhone.contains("*")

    var name by remember { mutableStateOf(origName) }
    var phone by remember { mutableStateOf(if (phoneEditable) origPhone else "") }
    var role by remember { mutableStateOf(origRole) }
    val origSuper = existing?.get("is_super_admin") == true
    var superAdmin by remember { mutableStateOf(origSuper) }
    var saving by remember { mutableStateOf(false) }
    var error by remember { mutableStateOf<String?>(null) }
    var matches by remember { mutableStateOf<List<Map<String, Any?>>>(emptyList()) }

    val digits = phone.filter { it.isDigit() }
    // Live duplicate check (debounced)
    LaunchedEffect(name, digits) {
        if (isEdit && name.trim() == origName && digits == origPhone.filter { it.isDigit() }) { matches = emptyList(); return@LaunchedEffect }
        if (name.trim().length < 2 && digits.length < 10) { matches = emptyList(); return@LaunchedEffect }
        delay(450)
        api.checkUser(name.trim().takeIf { it.length >= 2 }, digits.takeIf { it.length >= 10 })
            .onSuccess { list -> matches = list.filter { it["id"]?.toString() != existing?.get("id")?.toString() } }
    }

    val canChooseRole = prefs.isSuperAdmin && !targetIsSuper
    AlertDialog(
        onDismissRequest = { if (!saving) onDismiss() },
        title = { Text(if (isEdit) "Edit person" else "Add person", fontWeight = FontWeight.Bold) },
        text = {
            Column {
                OutlinedTextField(
                    value = name, onValueChange = { name = it; error = null },
                    label = { Text("Full name") }, singleLine = true, modifier = Modifier.fillMaxWidth()
                )
                Spacer(Modifier.height(8.dp))
                OutlinedTextField(
                    value = phone, onValueChange = { phone = it; error = null },
                    label = { Text("Phone number") },
                    placeholder = { Text(if (phoneEditable) "e.g. 9876543210" else origPhone) },
                    enabled = phoneEditable,
                    keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Phone),
                    singleLine = true, modifier = Modifier.fillMaxWidth()
                )
                if (matches.isNotEmpty()) {
                    Spacer(Modifier.height(8.dp))
                    Surface(color = WarningOrange.copy(alpha = 0.08f), shape = RoundedCornerShape(10.dp), border = BorderStroke(1.dp, WarningOrange.copy(alpha = 0.4f))) {
                        Column(Modifier.padding(10.dp)) {
                            Text("Possible duplicate", color = WarningOrange, fontWeight = FontWeight.Bold, fontSize = 13.sp)
                            matches.take(3).forEach { mt ->
                                val mName = mt["name"]?.toString() ?: "Someone"
                                val on = (mt["match_on"] as? List<*>)?.joinToString(" & ") ?: "details"
                                val archived = mt["is_archived"] == true
                                Text("• $mName · ${mt["phone"] ?: ""} (same $on${if (archived) ", archived" else ""})", fontSize = 12.sp)
                                if (archived && !isEdit) {
                                    TextButton(onClick = { onUnarchive(mt["id"].toString(), mName) }, contentPadding = PaddingValues(0.dp)) {
                                        Text("Un-archive $mName instead", color = JomiTeal, fontSize = 12.sp)
                                    }
                                }
                            }
                        }
                    }
                }
                if (canChooseRole) {
                    Spacer(Modifier.height(10.dp))
                    Text("Role", fontSize = 12.sp, fontWeight = FontWeight.SemiBold)
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        listOf("staff" to prefs.roleLabel, "admin" to "Admin").forEach { (value, label) ->
                            FilterChip(
                                selected = role == value, onClick = { role = value }, label = { Text(label) },
                                colors = FilterChipDefaults.filterChipColors(selectedContainerColor = JomiMint, selectedLabelColor = JomiTeal)
                            )
                        }
                    }
                } else if (isEdit) {
                    Spacer(Modifier.height(8.dp))
                    Text(
                        "Role: ${if (origRole == "admin") "Admin" else prefs.roleLabel}" + if (!prefs.isSuperAdmin) " (only a super admin can change roles)" else "",
                        color = JomiTextMuted, fontSize = 12.sp
                    )
                }
                // Super admin switch: visible only to super admins, only for admins
                if (isEdit && prefs.isSuperAdmin && role == "admin" && existing!!["id"]?.toString() != prefs.userId) {
                    Spacer(Modifier.height(8.dp))
                    Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.fillMaxWidth()) {
                        Column(Modifier.weight(1f)) {
                            Text("Super admin", fontWeight = FontWeight.SemiBold, fontSize = 14.sp)
                            Text("Can change roles and make other super admins", color = JomiTextMuted, fontSize = 12.sp)
                        }
                        Switch(checked = superAdmin, onCheckedChange = { superAdmin = it }, colors = SwitchDefaults.colors(checkedTrackColor = JomiGreen))
                    }
                }
                error?.let {
                    Spacer(Modifier.height(8.dp))
                    Text(it, color = RecordRed, fontSize = 12.sp)
                }
            }
        },
        confirmButton = {
            Button(
                enabled = !saving,
                colors = ButtonDefaults.buttonColors(containerColor = JomiGreen),
                onClick = {
                    val n = name.trim()
                    if (n.isEmpty()) { error = "Please enter a name"; return@Button }
                    if (phoneEditable && (!isEdit || digits.isNotEmpty()) && digits.length < 10) { error = "Enter at least 10 digits"; return@Button }
                    saving = true
                    scope.launch {
                        val res = if (isEdit) {
                            api.updateUser(
                                userId = existing!!["id"].toString(),
                                name = n,
                                role = role.takeIf { canChooseRole && it != origRole },
                                phone = digits.takeIf { phoneEditable && it.isNotEmpty() && it != origPhone.filter { c -> c.isDigit() } },
                                isSuperAdmin = superAdmin.takeIf { it != origSuper && role == "admin" }
                            )
                        } else {
                            api.createUser(n, digits, if (canChooseRole) role else "staff")
                        }
                        saving = false
                        if (res.isSuccess) {
                            if (isEdit && existing!!["id"]?.toString() == prefs.userId) prefs.userName = n
                            Toast.makeText(context, if (isEdit) "Saved" else "Added $n", Toast.LENGTH_SHORT).show()
                            onSaved()
                        } else {
                            error = res.exceptionOrNull()?.message ?: "Could not save"
                        }
                    }
                }
            ) { Text(if (saving) "Saving…" else "Save") }
        },
        dismissButton = { TextButton(onClick = onDismiss, enabled = !saving) { Text("Cancel") } }
    )
}

// ═════════════════════════════════════════════════════════════════════════════
// Lists (contract §3.3): Balgruha names and extra coach names
// ═════════════════════════════════════════════════════════════════════════════

@Composable
fun ListsSection(api: ISFApiClient, roleLabel: String) {
    var tab by remember { mutableStateOf(0) }
    JomiCard {
        SectionTitle("Balgruha", "Balgruha and coach names shown in the recording dropdowns")
        Spacer(Modifier.height(8.dp))
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            FilterChip(selected = tab == 0, onClick = { tab = 0 }, label = { Text("Balgruha") },
                colors = FilterChipDefaults.filterChipColors(selectedContainerColor = JomiMint, selectedLabelColor = JomiTeal))
            FilterChip(selected = tab == 1, onClick = { tab = 1 }, label = { Text("$roleLabel names") },
                colors = FilterChipDefaults.filterChipColors(selectedContainerColor = JomiMint, selectedLabelColor = JomiTeal))
        }
        Spacer(Modifier.height(8.dp))
        if (tab == 0) ListEditor(api, "balgruh", "Balgruha names")
        else ListEditor(api, "coach_name", "Extra $roleLabel names", "People without the app who can be picked as Assistant ${roleLabel.lowercase()}")
    }
}

/**
 * Admin: which template writes the report for each mode (round 3: Schedule -> Coach report,
 * Meeting -> Meeting minutes by default). Options = active /chat/templates; saved with PUT recorder-config.
 */
@Composable
fun ReportTemplatesSection(api: ISFApiClient, refreshKey: Int = 0, onTemplatesChanged: () -> Unit = {}) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    var options by remember { mutableStateOf<List<NameOption>>(emptyList()) }
    var current by remember { mutableStateOf<Map<String, String?>>(emptyMap()) }   // mode -> template id
    var saving by remember { mutableStateOf<String?>(null) }
    var error by remember { mutableStateOf<String?>(null) }

    fun parseCurrent(cfg: Map<String, Any?>): Map<String, String?> =
        ((cfg["report_templates"] as? Map<*, *>) ?: emptyMap<String, Any?>()).entries.associate { (k, v) ->
            k.toString() to ((v as? Map<*, *>)?.get("id")?.toString())
        }

    var restoring by remember { mutableStateOf(false) }
    LaunchedEffect(refreshKey) {
        error = null
        api.listChatTemplates()
            .onSuccess { list ->
                options = list.mapNotNull { t ->
                    val id = t["id"]?.toString() ?: return@mapNotNull null
                    NameOption(t["title"]?.toString()?.takeIf { it.isNotBlank() } ?: "Untitled", id)
                }.distinctBy { it.userId }
            }
            .onFailure { error = it.message }
        api.getRecorderConfig().onSuccess { current = parseCurrent(it) }.onFailure { error = error ?: it.message }
    }

    JomiCard {
        SectionTitle("Reports", "Which template writes the report after each recording")
        Spacer(Modifier.height(10.dp))
        error?.let { Text("Could not load: $it", color = RecordRed, fontSize = 12.sp); Spacer(Modifier.height(6.dp)) }
        listOf("schedule" to "Report for Schedule", "meeting" to "Report for Meeting").forEach { (mode, label) ->
            val selectedId = current[mode]
            val selected = options.firstOrNull { it.userId == selectedId }
            SelectDropdown(
                label = label,
                options = options,
                selected = selected,
                enabled = saving == null && options.isNotEmpty(),
                placeholder = if (options.isEmpty()) "Loading…" else "Select a template",
                onSelect = { opt ->
                    val id = opt.userId ?: return@SelectDropdown
                    if (id == selectedId) return@SelectDropdown
                    saving = mode
                    scope.launch {
                        api.updateRecorderConfig(reportTemplates = mapOf(mode to id))
                            .onSuccess {
                                current = parseCurrent(it)
                                Toast.makeText(context, "$label: ${opt.name}", Toast.LENGTH_SHORT).show()
                            }
                            .onFailure { Toast.makeText(context, it.message ?: "Could not save", Toast.LENGTH_LONG).show() }
                        saving = null
                    }
                },
                modifier = Modifier.fillMaxWidth()
            )
            Spacer(Modifier.height(10.dp))
        }
        Text("Recordings from the old app use the Schedule report.", color = JomiTextMuted, fontSize = 11.sp)
        Spacer(Modifier.height(8.dp))
        OutlinedButton(
            onClick = {
                restoring = true
                scope.launch {
                    api.restoreDefaultReportTemplates()
                        .onSuccess { res ->
                            @Suppress("UNCHECKED_CAST")
                            (res["config"] as? Map<String, Any?>)?.let { current = parseCurrent(it) }
                            val created = (res["created"] as? List<*>)?.joinToString(", ").orEmpty()
                            Toast.makeText(context, if (created.isBlank()) "The default templates are already there"
                                else "Restored: $created", Toast.LENGTH_LONG).show()
                            onTemplatesChanged()
                        }
                        .onFailure { Toast.makeText(context, it.message ?: "Could not restore", Toast.LENGTH_LONG).show() }
                    restoring = false
                }
            },
            enabled = !restoring && saving == null,
            border = BorderStroke(1.dp, JomiMintBorder),
            modifier = Modifier.fillMaxWidth()
        ) { Text(if (restoring) "Restoring…" else "Restore default report templates", color = JomiTeal) }
    }
}

@Composable
private fun ListEditor(api: ISFApiClient, listType: String, title: String, hint: String? = null) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    var items by remember { mutableStateOf<List<Map<String, Any?>>>(emptyList()) }
    var loading by remember { mutableStateOf(true) }
    var newName by remember { mutableStateOf("") }
    var adding by remember { mutableStateOf(false) }
    var renameTarget by remember { mutableStateOf<Map<String, Any?>?>(null) }

    fun load() {
        scope.launch {
            val res = api.listListItems(listType, includeInactive = true)
            loading = false
            res.onSuccess { items = it }.onFailure { Toast.makeText(context, "$title: ${it.message}", Toast.LENGTH_SHORT).show() }
        }
    }
    LaunchedEffect(listType) { load() }

    Text(title, fontWeight = FontWeight.SemiBold, fontSize = 14.sp)
    if (hint != null) Text(hint, color = JomiTextMuted, fontSize = 12.sp)
    Spacer(Modifier.height(6.dp))
    if (loading) {
        CircularProgressIndicator(Modifier.size(18.dp), color = JomiGreen, strokeWidth = 2.dp)
    } else if (items.isEmpty()) {
        Text("No names yet.", color = JomiTextMuted, fontSize = 13.sp)
    }
    items.forEach { item ->
        val id = item["id"]?.toString().orEmpty()
        val active = item["is_active"] != false
        Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.fillMaxWidth()) {
            Text(
                item["name"]?.toString() ?: "",
                fontSize = 14.sp,
                color = if (active) MaterialTheme.colorScheme.onBackground else JomiTextMuted,
                textDecoration = if (active) null else TextDecoration.LineThrough,
                modifier = Modifier.weight(1f)
            )
            SmallIconButton(R.drawable.ic_edit, "Rename", { renameTarget = item }, tint = JomiGreen, size = 32.dp)
            Switch(
                checked = active,
                onCheckedChange = { on ->
                    scope.launch {
                        api.updateListItem(listType, id, isActive = on)
                            .onSuccess { load() }
                            .onFailure { Toast.makeText(context, it.message ?: "Failed", Toast.LENGTH_SHORT).show() }
                    }
                },
                colors = SwitchDefaults.colors(checkedTrackColor = JomiGreen),
                modifier = Modifier.padding(start = 4.dp)
            )
        }
    }
    Spacer(Modifier.height(6.dp))
    Row(verticalAlignment = Alignment.CenterVertically) {
        OutlinedTextField(
            value = newName, onValueChange = { newName = it },
            placeholder = { Text("Add a name") }, singleLine = true,
            modifier = Modifier.weight(1f)
        )
        Spacer(Modifier.width(8.dp))
        Button(
            enabled = newName.isNotBlank() && !adding,
            colors = ButtonDefaults.buttonColors(containerColor = JomiGreen),
            onClick = {
                adding = true
                scope.launch {
                    val res = api.createListItem(listType, newName)
                    adding = false
                    res.onSuccess { newName = ""; load() }
                        .onFailure { Toast.makeText(context, it.message ?: "Could not add", Toast.LENGTH_LONG).show() }
                }
            }
        ) { Text("Add") }
    }

    renameTarget?.let { item ->
        EditTitleDialog(
            title = "Rename",
            initial = item["name"]?.toString().orEmpty(),
            onDismiss = { renameTarget = null },
            onSave = { n ->
                val res = api.updateListItem(listType, item["id"].toString(), name = n)
                if (res.isSuccess) { load(); null } else res.exceptionOrNull()?.message ?: "Could not rename"
            }
        )
    }
}

// ═════════════════════════════════════════════════════════════════════════════
// Unfinished sessions (admin): recordings whose app never called Stop/finish
// ═════════════════════════════════════════════════════════════════════════════

@Composable
fun UnfinishedSessionsSection(api: ISFApiClient) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    var items by remember { mutableStateOf<List<Map<String, Any?>>>(emptyList()) }
    var loading by remember { mutableStateOf(true) }
    var busyId by remember { mutableStateOf<String?>(null) }
    var finishTarget by remember { mutableStateOf<Map<String, Any?>?>(null) }
    var discardTarget by remember { mutableStateOf<Map<String, Any?>?>(null) }

    fun load() {
        scope.launch {
            loading = true
            val res = api.listUnfinishedSessions()
            loading = false
            res.onSuccess { items = it }
                .onFailure { Toast.makeText(context, "Unfinished sessions: ${it.message}", Toast.LENGTH_SHORT).show() }
        }
    }
    LaunchedEffect(Unit) { load() }

    JomiCard {
        SectionTitle("Unfinished sessions", "Recordings that were never stopped properly (app closed or phone switched off)") {
            SmallIconButton(R.drawable.ic_refresh, "Refresh", { load() }, tint = JomiGreen, size = 32.dp)
        }
        Spacer(Modifier.height(8.dp))
        when {
            loading -> CircularProgressIndicator(Modifier.size(18.dp), color = JomiGreen, strokeWidth = 2.dp)
            items.isEmpty() -> Text("Nothing unfinished.", color = JomiTextMuted, fontSize = 13.sp)
        }
        items.forEach { s ->
            val id = s["id"]?.toString().orEmpty()
            val audioSec = (s["audio_seconds"] as? Number)?.toInt() ?: 0
            val chunks = (s["chunks"] as? Number)?.toInt() ?: 0
            Column(Modifier.fillMaxWidth().padding(vertical = 6.dp)) {
                Text(
                    s["title"]?.toString()?.takeIf { it.isNotBlank() && it != "null" } ?: (s["recorded_by"]?.toString() ?: "Recording"),
                    fontWeight = FontWeight.SemiBold, fontSize = 14.sp, maxLines = 1, overflow = TextOverflow.Ellipsis
                )
                Text(
                    "${s["recorded_by"] ?: ""} · ${shortWhen(s["started_at"])} · " +
                        if (chunks > 0) "${humanDuration(audioSec)} of audio uploaded" else "no audio uploaded",
                    color = JomiTextMuted, fontSize = 12.sp, maxLines = 1, overflow = TextOverflow.Ellipsis
                )
                Row(Modifier.padding(top = 6.dp)) {
                    OutlinedButton(
                        enabled = busyId == null,
                        onClick = { finishTarget = s },
                        border = BorderStroke(1.dp, JomiGreen)
                    ) { Text(if (chunks > 0) "Finish & transcribe" else "Close it", color = JomiGreen, fontSize = 13.sp) }
                    Spacer(Modifier.width(8.dp))
                    OutlinedButton(
                        enabled = busyId == null,
                        onClick = { discardTarget = s },
                        border = BorderStroke(1.dp, MaterialTheme.colorScheme.error)
                    ) { Text("Discard", color = MaterialTheme.colorScheme.error, fontSize = 13.sp) }
                    if (busyId == id) {
                        Spacer(Modifier.width(8.dp))
                        CircularProgressIndicator(Modifier.size(18.dp).align(Alignment.CenterVertically), color = JomiGreen, strokeWidth = 2.dp)
                    }
                }
            }
            HorizontalDivider(color = JomiBorder)
        }
    }

    finishTarget?.let { s ->
        val id = s["id"].toString()
        val chunks = (s["chunks"] as? Number)?.toInt() ?: 0
        AlertDialog(
            onDismissRequest = { finishTarget = null },
            title = { Text(if (chunks > 0) "Finish and transcribe?" else "Close this session?") },
            text = {
                Text(
                    if (chunks > 0) "The audio that reached the server (${humanDuration((s["audio_seconds"] as? Number)?.toInt() ?: 0)}) " +
                        "will be saved and transcribed. This uses AI credits like a normal recording."
                    else "No audio reached the server, so it will just be marked as abandoned. No AI cost."
                )
            },
            confirmButton = {
                TextButton(onClick = {
                    finishTarget = null
                    busyId = id
                    scope.launch {
                        val res = api.forceFinishSession(id)
                        busyId = null
                        res.onSuccess { Toast.makeText(context, "Done", Toast.LENGTH_SHORT).show(); load() }
                            .onFailure { Toast.makeText(context, it.message ?: "Could not finish", Toast.LENGTH_LONG).show() }
                    }
                }) { Text(if (chunks > 0) "Finish & transcribe" else "Close it", color = JomiGreen) }
            },
            dismissButton = { TextButton(onClick = { finishTarget = null }) { Text("Cancel") } }
        )
    }

    discardTarget?.let { s ->
        val id = s["id"].toString()
        StrongDeleteDialog(
            itemTitle = s["title"]?.toString()?.takeIf { it.isNotBlank() && it != "null" } ?: "Unfinished recording",
            busy = busyId == id,
            onDismiss = { discardTarget = null },
            onConfirm = {
                busyId = id
                scope.launch {
                    val res = api.eraseRecording(id)
                    busyId = null
                    discardTarget = null
                    res.onSuccess { load() }
                        .onFailure { Toast.makeText(context, it.message ?: "Could not discard", Toast.LENGTH_LONG).show() }
                }
            }
        )
    }
}

// ═════════════════════════════════════════════════════════════════════════════
// Profile, Ask Jomi row, About
// ═════════════════════════════════════════════════════════════════════════════

@Composable
fun ProfileCard(prefs: UserPreferences, onSwitchView: () -> Unit, onSignOut: () -> Unit) {
    var confirmSignOut by remember { mutableStateOf(false) }
    JomiCard {
        SectionTitle("Profile")
        Spacer(Modifier.height(8.dp))
        Row(verticalAlignment = Alignment.CenterVertically) {
            Box(Modifier.size(44.dp).background(JomiMint, CircleShape), contentAlignment = Alignment.Center) {
                Text(initialsOf(prefs.userName), color = JomiTeal, fontWeight = FontWeight.Bold)
            }
            Spacer(Modifier.width(12.dp))
            Column(Modifier.weight(1f)) {
                Text(prefs.userName ?: "—", fontWeight = FontWeight.SemiBold, fontSize = 17.sp, maxLines = 1, overflow = TextOverflow.Ellipsis)
                Text(
                    listOfNotNull(prefs.displayRole, if (prefs.isSuperAdmin) "Super admin" else null, prefs.orgName).joinToString(" · "),
                    color = JomiTextMuted, fontSize = 13.sp, maxLines = 2, overflow = TextOverflow.Ellipsis
                )
                prefs.userPhone?.let { Text(it, color = JomiTextMuted, fontSize = 12.sp, maxLines = 1) }
            }
        }
        if (prefs.isSuperAdmin && prefs.isRealAdmin) {
            Spacer(Modifier.height(10.dp))
            Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.fillMaxWidth()) {
                Column(Modifier.weight(1f)) {
                    Text("View as ${prefs.roleLabel}", fontWeight = FontWeight.Medium, fontSize = 14.sp)
                    Text("See the app exactly as a ${prefs.roleLabel.lowercase()} does", color = JomiTextMuted, fontSize = 12.sp)
                }
                Switch(checked = prefs.viewAsCoach, onCheckedChange = { onSwitchView() }, colors = SwitchDefaults.colors(checkedTrackColor = JomiGreen))
            }
        }
        Spacer(Modifier.height(12.dp))
        OutlinedButton(
            onClick = { confirmSignOut = true },
            border = BorderStroke(1.dp, RecordRed.copy(alpha = 0.4f)),
            colors = ButtonDefaults.outlinedButtonColors(contentColor = RecordRed),
            modifier = Modifier.fillMaxWidth()
        ) { Text("Sign out", fontWeight = FontWeight.SemiBold) }
    }
    if (confirmSignOut) {
        AlertDialog(
            onDismissRequest = { confirmSignOut = false },
            title = { Text("Sign out?") },
            text = { Text("Recordings already saved on this phone will still upload after you sign in again.") },
            confirmButton = { TextButton(onClick = { confirmSignOut = false; onSignOut() }) { Text("Sign out", color = RecordRed) } },
            dismissButton = { TextButton(onClick = { confirmSignOut = false }) { Text("Cancel") } }
        )
    }
}

@Composable
fun SettingsNavRow(title: String, subtitle: String?, onClick: () -> Unit) {
    JomiCard(modifier = Modifier.clickable(onClick = onClick), padding = 14.dp) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Icon(androidx.compose.ui.res.painterResource(R.drawable.ic_sparkle), null, tint = JomiTeal, modifier = Modifier.size(22.dp))
            Spacer(Modifier.width(12.dp))
            Column(Modifier.weight(1f)) {
                Text(title, fontWeight = FontWeight.SemiBold, fontSize = 15.sp)
                if (subtitle != null) Text(subtitle, color = JomiTextMuted, fontSize = 12.sp, maxLines = 1, overflow = TextOverflow.Ellipsis)
            }
            Icon(
                androidx.compose.ui.res.painterResource(R.drawable.ic_expand_more), null, tint = JomiTextMuted,
                modifier = Modifier.size(22.dp).graphicsLayer(rotationZ = -90f)
            )
        }
    }
}

@Composable
fun AboutCard(syncTriggered: Boolean, onSync: () -> Unit) {
    val context = LocalContext.current
    JomiCard {
        SectionTitle("About")
        Spacer(Modifier.height(8.dp))
        Text(
            "ISF Recorder v${com.isf.recorder.BuildConfig.VERSION_NAME} (build ${com.isf.recorder.BuildConfig.VERSION_CODE})",
            fontSize = 14.sp, fontWeight = FontWeight.Medium
        )
        Text("Audio, transcripts, reports and chats are kept until you erase them. Silence is not saved.", color = JomiTextMuted, fontSize = 12.sp)
        TextButton(onClick = {
            try {
                context.startActivity(android.content.Intent(android.content.Intent.ACTION_VIEW, android.net.Uri.parse("https://gubandhu.initiativesewafoundation.com/recorder/apk")))
            } catch (e: Exception) {
                Toast.makeText(context, "Could not open browser", Toast.LENGTH_SHORT).show()
            }
        }, contentPadding = PaddingValues(0.dp)) { Text("Download latest app", color = JomiTeal) }
        HorizontalDivider(color = JomiBorder)
        Row(
            verticalAlignment = Alignment.CenterVertically,
            modifier = Modifier.fillMaxWidth().clickable(onClick = onSync).padding(vertical = 10.dp)
        ) {
            Icon(androidx.compose.ui.res.painterResource(R.drawable.ic_refresh), null, tint = JomiTextMuted, modifier = Modifier.size(18.dp))
            Spacer(Modifier.width(8.dp))
            Text(
                if (syncTriggered) "Upload queued" else "Upload waiting recordings now",
                fontSize = 13.sp, color = JomiTextMuted, modifier = Modifier.weight(1f)
            )
        }
    }
}

/**
 * Settings v2.1: one compact row (icon · title · muted summary · chevron) that expands its content IN
 * PLACE below it. The screen keeps only one section open at a time (the caller passes [open]).
 * [navigates] rows (Ask Jomi) open their own screen instead.
 */
@Composable
fun SettingsSection(
    icon: String,
    title: String,
    summary: String?,
    open: Boolean,
    onToggle: () -> Unit,
    navigates: Boolean = false,
    alwaysOpen: Boolean = false,      // small, often-used sections: no tap needed, content always shown
    content: @Composable () -> Unit = {}
) {
    val isOpen = alwaysOpen || open
    Column(Modifier.fillMaxWidth().padding(bottom = 8.dp)) {
        Surface(
            color = JomiCard,
            shape = RoundedCornerShape(14.dp),
            border = BorderStroke(1.dp, if (isOpen) JomiMintBorder else JomiBorder),
            modifier = Modifier.fillMaxWidth().clickable(enabled = !alwaysOpen, onClick = onToggle)
        ) {
            Row(Modifier.padding(horizontal = 14.dp, vertical = 12.dp), verticalAlignment = Alignment.CenterVertically) {
                Text(icon, fontSize = 18.sp)
                Spacer(Modifier.width(10.dp))
                Text(title, fontWeight = FontWeight.SemiBold, fontSize = 15.sp, maxLines = 1,
                    overflow = TextOverflow.Ellipsis, modifier = Modifier.weight(1f, fill = false))
                Spacer(Modifier.width(8.dp))
                Text(summary.orEmpty(), color = JomiTextMuted, fontSize = 12.sp, maxLines = 1, overflow = TextOverflow.Ellipsis,
                    textAlign = androidx.compose.ui.text.style.TextAlign.End, modifier = Modifier.weight(1f))
                Spacer(Modifier.width(6.dp))
                val rotation by androidx.compose.animation.core.animateFloatAsState(if (isOpen) 90f else 0f, label = "chevron")
                if (!alwaysOpen) Text("›", color = JomiTextMuted, fontSize = 20.sp,
                    modifier = Modifier.graphicsLayer { rotationZ = if (navigates) 0f else rotation })
            }
        }
        if (!navigates) {
            androidx.compose.animation.AnimatedVisibility(visible = isOpen) {
                Column(Modifier.fillMaxWidth().padding(top = 8.dp)) {
                    content()
                    // Close long sections from the bottom, without scrolling back up to the header
                    if (!alwaysOpen) TextButton(onClick = onToggle, modifier = Modifier.fillMaxWidth()) {
                        Text("▲  Minimize $title", color = JomiTeal, fontWeight = FontWeight.Bold)
                    }
                }
            }
        }
    }
}

@androidx.compose.ui.tooling.preview.Preview(name = "Settings menu 360dp x1.3", showBackground = true, widthDp = 360, fontScale = 1.3f)
@Composable
private fun SettingsMenuPreview() {
    com.isf.recorder.ui.theme.ISFRecorderTheme {
        Column(Modifier.padding(16.dp)) {
            SettingsSection("👤", "Profile", "Rizwana Shaikh · Super admin", open = false, onToggle = {})
            SettingsSection("💬", "Ask Jomi", "Saved conversations", open = false, onToggle = {}, navigates = true)
            SettingsSection("👥", "Team", "16 people", open = false, onToggle = {})
            SettingsSection("🏠", "Balgruha", "15 Balgruha · 8 names", open = true, onToggle = {}) {
                Text("…section content…", color = JomiTextMuted)
            }
            SettingsSection("📝", "Reports", "Coach / Meeting minutes", open = false, onToggle = {})
            SettingsSection("🤖", "AI model", "Gemini 2.5 Flash", open = false, onToggle = {})
            SettingsSection("⏸", "Unfinished sessions", "None", open = false, onToggle = {})
            SettingsSection("ℹ️", "About", "v2.1.1", open = false, onToggle = {})
        }
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// Silence auto-stop (admin, 13 Oct): minutes without voice before a recording stops, per mode
// ─────────────────────────────────────────────────────────────────────────────
@Composable
fun SilenceStopSection(api: ISFApiClient) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    var schedule by remember { mutableStateOf("") }
    var meeting by remember { mutableStateOf("") }
    var loaded by remember { mutableStateOf(false) }
    var saving by remember { mutableStateOf(false) }
    LaunchedEffect(Unit) {
        api.getRecorderConfig().onSuccess { cfg ->
            val m = cfg?.get("silence_stop_minutes") as? Map<*, *>
            schedule = ((m?.get("schedule") as? Number)?.toInt() ?: 0).toString()
            meeting = ((m?.get("meeting") as? Number)?.toInt() ?: 0).toString()
        }
        loaded = true
    }
    fun minutes(v: String) = v.trim().toIntOrNull()
    Column {
        Text("If nobody speaks for this many minutes, the recording stops by itself and the report is made. " +
            "One minute before, the phone says \"Recording will stop in one minute\" — any voice cancels it. 0 = never stop.",
            color = JomiTextMuted, fontSize = 13.sp)
        Spacer(Modifier.height(12.dp))
        Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
            listOf("Schedule" to schedule, "Meeting" to meeting).forEachIndexed { i, (label, value) ->
                OutlinedTextField(
                    value = value,
                    onValueChange = { t -> val d = t.filter { it.isDigit() }.take(3); if (i == 0) schedule = d else meeting = d },
                    label = { Text("$label (min)") },
                    singleLine = true,
                    enabled = loaded && !saving,
                    isError = minutes(value)?.let { it > 180 } ?: value.isNotEmpty(),
                    keyboardOptions = androidx.compose.foundation.text.KeyboardOptions(keyboardType = androidx.compose.ui.text.input.KeyboardType.Number),
                    modifier = Modifier.weight(1f)
                )
            }
        }
        Spacer(Modifier.height(10.dp))
        val s = minutes(schedule); val m = minutes(meeting)
        Button(
            enabled = loaded && !saving && s != null && m != null && s <= 180 && m <= 180,
            colors = ButtonDefaults.buttonColors(containerColor = JomiGreen),
            onClick = {
                saving = true
                scope.launch {
                    val r = api.updateRecorderConfig(silenceStopMinutes = mapOf("schedule" to (s ?: 0), "meeting" to (m ?: 0)))
                    saving = false
                    r.onSuccess { Toast.makeText(context, "Saved. Applies to recordings started from now.", Toast.LENGTH_SHORT).show() }
                        .onFailure { Toast.makeText(context, it.message ?: "Could not save", Toast.LENGTH_LONG).show() }
                }
            },
            modifier = Modifier.fillMaxWidth()
        ) { Text(if (saving) "Saving…" else "Save") }
    }
}
