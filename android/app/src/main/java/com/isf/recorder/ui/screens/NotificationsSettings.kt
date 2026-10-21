package com.isf.recorder.ui.screens

import android.widget.Toast
import androidx.compose.foundation.layout.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.isf.recorder.data.api.ISFApiClient
import com.isf.recorder.data.prefs.UserPreferences
import com.isf.recorder.ui.theme.*
import kotlinx.coroutines.launch

/**
 * Settings → 🔔 Notifications (21 Oct): the person's own on/off switches (only the ones for their role, from
 * the server), and for super admins a box to send a message to everyone / coaches / admins.
 */
@Composable
fun NotificationsSettingsSection(api: ISFApiClient) {
    val context = LocalContext.current
    val prefs = remember { UserPreferences(context) }
    val scope = rememberCoroutineScope()
    var items by remember { mutableStateOf<List<Map<String, Any?>>>(emptyList()) }
    var canAnnounce by remember { mutableStateOf(false) }
    var error by remember { mutableStateOf<String?>(null) }

    fun apply(res: Map<String, Any?>) {
        @Suppress("UNCHECKED_CAST")
        items = (res["items"] as? List<Map<String, Any?>>).orEmpty()
        canAnnounce = res["can_announce"] as? Boolean ?: false
        items.firstOrNull { it["kind"] == "upload_stuck" }?.let { prefs.notifyUploadStuck = it["on"] as? Boolean ?: true }
    }
    LaunchedEffect(Unit) {
        api.getNotificationSettings().onSuccess { apply(it); error = null }.onFailure { error = it.message ?: "Could not load" }
    }

    Column {
        Text("Jomi checks for new notifications every 15 minutes and whenever you open the app.",
            color = JomiTextMuted, fontSize = 12.sp)
        error?.let { Text("⚠️ $it", color = RecordRed, fontSize = 12.sp) }
        items.forEach { item ->
            val kind = item["kind"]?.toString().orEmpty()
            Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.padding(top = 6.dp)) {
                Text(item["label"]?.toString().orEmpty(), fontSize = 14.sp, modifier = Modifier.weight(1f))
                Switch(checked = item["on"] as? Boolean ?: true, onCheckedChange = { on ->
                    items = items.map { if (it["kind"] == kind) it + ("on" to on) else it }
                    scope.launch {
                        api.setNotificationSettings(mapOf(kind to on)).onSuccess { apply(it) }
                            .onFailure { Toast.makeText(context, it.message ?: "Could not save", Toast.LENGTH_SHORT).show() }
                    }
                }, colors = SwitchDefaults.colors(checkedTrackColor = JomiGreen))
            }
        }
        Text("Messages from admins always arrive.", color = JomiTextMuted, fontSize = 12.sp, modifier = Modifier.padding(top = 6.dp))

        if (canAnnounce) {
            HorizontalDivider(Modifier.padding(vertical = 12.dp), color = JomiBorder)
            AnnouncementBox(api)
        }
    }
}

@Composable
private fun AnnouncementBox(api: ISFApiClient) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    var message by remember { mutableStateOf("") }
    var audience by remember { mutableStateOf("all") }
    var confirm by remember { mutableStateOf(false) }
    var sending by remember { mutableStateOf(false) }
    val audiences = listOf("all" to "Everyone", "coaches" to "Coaches", "admins" to "Admins")

    Text("📢 Send a message to the team", fontWeight = FontWeight.SemiBold, fontSize = 15.sp)
    Text("Everyone you choose gets it as a phone notification.", color = JomiTextMuted, fontSize = 12.sp)
    Spacer(Modifier.height(6.dp))
    OutlinedTextField(value = message, onValueChange = { message = it.take(1000) }, modifier = Modifier.fillMaxWidth(),
        placeholder = { Text("e.g. Team meeting tomorrow at 4 PM in the office") }, minLines = 2, maxLines = 6)
    Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.padding(top = 6.dp)) {
        audiences.forEach { (code, label) ->
            FilterChip(selected = audience == code, onClick = { audience = code }, label = { Text(label, fontSize = 12.sp) },
                modifier = Modifier.padding(end = 6.dp))
        }
    }
    Button(onClick = { confirm = true }, enabled = message.isNotBlank() && !sending,
        colors = ButtonDefaults.buttonColors(containerColor = JomiGreen), modifier = Modifier.fillMaxWidth()) {
        Text(if (sending) "Sending…" else "Send")
    }
    if (confirm) {
        val who = audiences.first { it.first == audience }.second.lowercase()
        AlertDialog(
            onDismissRequest = { confirm = false },
            title = { Text("Send to $who?") },
            text = { Text(message.trim()) },
            confirmButton = {
                Button(onClick = {
                    confirm = false
                    sending = true
                    scope.launch {
                        api.sendAnnouncement(message.trim(), audience)
                            .onSuccess { n ->
                                Toast.makeText(context, "Sent to $n ${if (n == 1) "person" else "people"}", Toast.LENGTH_LONG).show()
                                message = ""
                            }
                            .onFailure { Toast.makeText(context, it.message ?: "Could not send", Toast.LENGTH_LONG).show() }
                        sending = false
                    }
                }, colors = ButtonDefaults.buttonColors(containerColor = JomiGreen)) { Text("Send") }
            },
            dismissButton = { TextButton(onClick = { confirm = false }) { Text("Cancel") } }
        )
    }
}
