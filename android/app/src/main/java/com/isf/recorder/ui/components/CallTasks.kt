package com.isf.recorder.ui.components

import android.widget.Toast
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextDecoration
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.isf.recorder.data.api.ISFApiClient
import com.isf.recorder.ui.theme.*
import kotlinx.coroutines.launch

/**
 * Tasks from calls (14 Oct): tick done, tap to edit, 🗑 delete, "+ Add task". Used in a call's popup
 * ([recordingId] set) and in "My tasks" (all calls, [recordingId] = null).
 */
@Composable
fun TasksPanel(api: ISFApiClient, recordingId: String?, status: String = "open", showCall: Boolean = false,
               onChanged: () -> Unit = {}) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    var items by remember(recordingId, status) { mutableStateOf<List<Map<String, Any?>>>(emptyList()) }
    var loading by remember(recordingId, status) { mutableStateOf(true) }
    var editing by remember { mutableStateOf<Map<String, Any?>?>(null) }
    var adding by remember { mutableStateOf(false) }

    suspend fun load() {
        api.listTasks(if (recordingId != null) "all" else status, recordingId)
            .onSuccess { items = it }.onFailure { Toast.makeText(context, it.message ?: "Could not load tasks", Toast.LENGTH_SHORT).show() }
        loading = false
    }
    LaunchedEffect(recordingId, status) { load() }
    fun act(block: suspend () -> Result<*>) = scope.launch {
        block().onSuccess { load(); onChanged() }.onFailure { Toast.makeText(context, it.message ?: "Could not save", Toast.LENGTH_SHORT).show() }
    }

    Column(Modifier.fillMaxWidth()) {
        when {
            loading -> CircularProgressIndicator(Modifier.size(18.dp), color = JomiGreen, strokeWidth = 2.dp)
            items.isEmpty() -> Text(if (recordingId != null) "No tasks from this call." else if (status == "done") "Nothing done yet." else "No open tasks 🎉",
                color = JomiTextMuted, fontSize = 13.sp)
        }
        items.forEach { t ->
            val id = t["id"].toString()
            val done = t["done"] as? Boolean ?: false
            Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.fillMaxWidth().padding(vertical = 2.dp)) {
                Checkbox(checked = done, onCheckedChange = { v -> act { api.updateTask(id, done = v) } },
                    colors = CheckboxDefaults.colors(checkedColor = JomiGreen))
                Column(Modifier.weight(1f).clickable { editing = t }) {
                    Text(t["text"]?.toString().orEmpty(), fontSize = 14.sp,
                        textDecoration = if (done) TextDecoration.LineThrough else null,
                        color = if (done) JomiTextMuted else MaterialTheme.colorScheme.onBackground)
                    val meta = listOfNotNull(
                        t["assigned_to"]?.toString()?.takeIf { it.isNotBlank() && it != "null" }?.let { "👤 $it" },
                        t["due_text"]?.toString()?.takeIf { it.isNotBlank() && it != "null" }?.let { "⏰ $it" },
                        if (showCall) t["contact_name"]?.toString()?.takeIf { it.isNotBlank() && it != "null" }?.let { "📞 $it" } else null
                    )
                    if (meta.isNotEmpty()) Text(meta.joinToString("  ·  "), color = JomiTextMuted, fontSize = 12.sp)
                }
                TextButton(onClick = { act { api.deleteTask(id) } }, contentPadding = PaddingValues(4.dp)) { Text("🗑", fontSize = 16.sp) }
            }
        }
        TextButton(onClick = { adding = true }) { Text("+ Add task", color = JomiTeal, fontWeight = FontWeight.SemiBold) }
    }

    editing?.let { t ->
        TaskDialog("Edit task", t["text"]?.toString().orEmpty(), t["due_text"]?.toString()?.takeIf { it != "null" }.orEmpty(),
            onDismiss = { editing = null }) { text, due ->
            editing = null
            act { api.updateTask(t["id"].toString(), text = text, dueText = due) }
        }
    }
    if (adding) {
        TaskDialog("Add task", "", "", onDismiss = { adding = false }) { text, due ->
            adding = false
            act { api.addTask(text, due.ifBlank { null }, recordingId) }
        }
    }
}

@Composable
private fun TaskDialog(title: String, text0: String, due0: String, onDismiss: () -> Unit, onSave: (String, String) -> Unit) {
    var text by remember { mutableStateOf(text0) }
    var due by remember { mutableStateOf(due0) }
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text(title, fontWeight = FontWeight.Bold) },
        text = {
            Column {
                OutlinedTextField(text, { text = it }, label = { Text("Task") }, modifier = Modifier.fillMaxWidth())
                Spacer(Modifier.height(8.dp))
                OutlinedTextField(due, { due = it }, label = { Text("By when (optional)") }, singleLine = true,
                    placeholder = { Text("e.g. Friday") }, modifier = Modifier.fillMaxWidth())
            }
        },
        confirmButton = {
            Button(onClick = { onSave(text.trim(), due.trim()) }, enabled = text.isNotBlank(),
                colors = ButtonDefaults.buttonColors(containerColor = JomiGreen)) { Text("Save") }
        },
        dismissButton = { TextButton(onClick = onDismiss) { Text("Cancel") } }
    )
}

/** "My tasks": all tasks from my calls, Open / Done. */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun MyTasksSheet(api: ISFApiClient, onDismiss: () -> Unit) {
    var tab by remember { mutableStateOf("open") }
    ModalBottomSheet(onDismissRequest = onDismiss, containerColor = MaterialTheme.colorScheme.background) {
        Column(Modifier.fillMaxWidth().padding(horizontal = 16.dp).padding(bottom = 24.dp).verticalScroll(rememberScrollState())) {
            Text("✅ My tasks", fontWeight = FontWeight.Bold, fontSize = 18.sp)
            Text("From your calls. Only you can see these.", color = JomiTextMuted, fontSize = 12.sp)
            Spacer(Modifier.height(10.dp))
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                listOf("open" to "Open", "done" to "Done").forEach { (k, label) ->
                    FilterChip(selected = tab == k, onClick = { tab = k }, label = { Text(label) })
                }
            }
            Spacer(Modifier.height(6.dp))
            TasksPanel(api, recordingId = null, status = tab, showCall = true)
        }
    }
}
