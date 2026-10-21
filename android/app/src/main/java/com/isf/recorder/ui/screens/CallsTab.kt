package com.isf.recorder.ui.screens

import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.isf.recorder.data.api.ISFApiClient
import com.isf.recorder.data.prefs.UserPreferences
import com.isf.recorder.sync.CallSync
import com.isf.recorder.ui.components.*
import com.isf.recorder.ui.theme.*
import kotlinx.coroutines.launch

/**
 * Home → Calls tab (14 Oct): my synced phone calls (private), "My tasks", search by contact + date.
 * Admins also get "Shared with me" (calls their owners shared). Tapping a call opens its report popup.
 */
@Composable
fun CallsTab(api: ISFApiClient, isAdmin: Boolean, refreshKey: Int, onOpenCall: (Map<String, Any?>) -> Unit,
             onOpenSettings: () -> Unit) {
    val context = LocalContext.current
    val prefs = remember { UserPreferences(context) }
    var shared by remember { mutableStateOf(false) }
    var contact by remember { mutableStateOf<String?>(null) }
    var date by remember { mutableStateOf(DateFilter.ALL) }
    var contacts by remember { mutableStateOf<List<String>>(emptyList()) }
    var items by remember { mutableStateOf<List<Map<String, Any?>>>(emptyList()) }
    var total by remember { mutableStateOf(0) }
    var page by remember { mutableStateOf(1) }
    var loading by remember { mutableStateOf(true) }
    var error by remember { mutableStateOf<String?>(null) }
    var openTasks by remember { mutableStateOf(0) }
    var showTasks by remember { mutableStateOf(false) }

    suspend fun load(reset: Boolean) {
        val p = if (reset) 1 else page + 1
        loading = true
        api.listCalls(page = p, contact = contact, dateFrom = date.fromParam, dateTo = date.toParam, sharedOnly = shared)
            .onSuccess { d ->
                @Suppress("UNCHECKED_CAST")
                val got = (d["items"] as? List<Map<String, Any?>>) ?: emptyList()
                total = (d["total"] as? Number)?.toInt() ?: got.size
                items = if (reset) got else items + got
                page = p
                error = null
            }.onFailure { error = it.message }
        loading = false
    }
    LaunchedEffect(shared, contact, date, refreshKey) { load(reset = true) }
    LaunchedEffect(refreshKey) {
        api.callContacts().onSuccess { list -> contacts = list.mapNotNull { it["name"]?.toString() } }
        api.getTaskReminder().onSuccess { openTasks = (it["open_count"] as? Number)?.toInt() ?: 0 }
        if (prefs.callSyncOn) CallSync.syncNow(context)          // look for new calls whenever the tab opens
    }

    Column(Modifier.fillMaxWidth()) {
        if (!prefs.callSyncOn) {
            JomiCard {
                Text("📞 Call sync is off", fontWeight = FontWeight.Bold)
                Text("Turn on \"Sync my call recordings\" in Settings → Calls. Calls recorded by your phone will " +
                    "then appear here with a summary and tasks — only you can see them.", color = JomiTextMuted, fontSize = 13.sp)
                TextButton(onClick = onOpenSettings) { Text("Open Settings → Calls", color = JomiTeal, fontWeight = FontWeight.SemiBold) }
            }
            Spacer(Modifier.height(10.dp))
        }
        OutlinedButton(onClick = { showTasks = true }, border = BorderStroke(1.dp, JomiGreen), modifier = Modifier.fillMaxWidth()) {
            Text("✅ My tasks" + if (openTasks > 0) " ($openTasks open)" else "", color = JomiTeal, fontWeight = FontWeight.Bold)
        }
        Spacer(Modifier.height(10.dp))
        if (isAdmin) {
            ModeToggle(listOf("mine", "shared"), if (shared) "shared" else "mine", { shared = it == "shared" },
                labels = mapOf("mine" to "My calls", "shared" to "Shared with me"))
            Spacer(Modifier.height(8.dp))
        }
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp), modifier = Modifier.fillMaxWidth()) {
            if (!shared) FilterChipMenu("Contact", contact, contacts, { contact = it }, modifier = Modifier.weight(1f))
            DateFilterChip(date, { date = it }, modifier = Modifier.weight(1f))
        }
        if (contact != null || date != DateFilter.ALL) {
            TextButton(onClick = { contact = null; date = DateFilter.ALL }, contentPadding = PaddingValues(horizontal = 4.dp, vertical = 0.dp)) {
                Text("✕ Clear filters", color = JomiTeal, fontSize = 13.sp)
            }
        }
        Spacer(Modifier.height(8.dp))
        when {
            items.isEmpty() && loading -> CircularProgressIndicator(Modifier.size(22.dp), color = JomiGreen, strokeWidth = 2.dp)
            items.isEmpty() -> Text(error?.let { "Could not load calls: $it" }
                ?: if (shared) "No calls have been shared with you." else "No calls yet.", color = JomiTextMuted, fontSize = 13.sp)
        }
        Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
            items.forEach { c -> key(c["id"]) { CallCard(c, showOwner = shared) { onOpenCall(c) } } }
        }
        if (items.size < total && !loading) {
            val scope = rememberCoroutineScope()
            OutlinedButton(onClick = { scope.launch { load(reset = false) } }, modifier = Modifier.fillMaxWidth().padding(top = 10.dp)) {
                Text("Load older (${items.size} of $total)", color = JomiTeal)
            }
        }
    }
    if (showTasks) MyTasksSheet(api) { showTasks = false }
}

@Composable
private fun CallCard(c: Map<String, Any?>, showOwner: Boolean, onClick: () -> Unit) {
    fun s(k: String) = c[k]?.toString()?.takeIf { it.isNotBlank() && it != "null" }
    val who = s("contact_name") ?: s("contact_phone") ?: "Unknown"
    val report = s("report_status")
    val status = when {
        s("status") == "error" -> "⚠️ Could not process"
        report == "draft" || report == "final" -> "📝 Summary ready"
        s("status") == "completed" -> "✓ Transcribed"
        else -> "⏳ Processing…"
    }
    val open = (c["open_tasks"] as? Number)?.toInt() ?: 0
    Surface(onClick = onClick, shape = RoundedCornerShape(14.dp), color = JomiCard, border = BorderStroke(1.dp, JomiBorder),
        modifier = Modifier.fillMaxWidth()) {
        Column(Modifier.padding(14.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text("📞 $who", fontWeight = FontWeight.SemiBold, fontSize = 15.sp, maxLines = 1, overflow = TextOverflow.Ellipsis,
                    modifier = Modifier.weight(1f))
                if (c["shared_with_admin"] == true && !showOwner) Text("👁 Shared", color = JomiTeal, fontSize = 11.sp)
            }
            Text(listOfNotNull(recordingDate(c).takeIf { it.isNotBlank() }, s("duration_formatted")).joinToString(" · "),
                color = JomiTextMuted, fontSize = 12.sp)
            if (showOwner) s("user_name")?.let { Text("🎙️ $it", color = JomiTeal, fontSize = 12.sp) }
            Row {
                Text(status, color = JomiTextMuted, fontSize = 12.sp, modifier = Modifier.weight(1f))
                if (open > 0) Text("✅ $open open task${if (open == 1) "" else "s"}", color = JomiTeal, fontSize = 12.sp, fontWeight = FontWeight.SemiBold)
            }
        }
    }
}
