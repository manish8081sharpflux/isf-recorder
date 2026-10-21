package com.isf.recorder.ui.screens

import android.widget.Toast
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.isf.recorder.R
import com.isf.recorder.data.api.ISFApiClient
import com.isf.recorder.ui.components.*
import com.isf.recorder.ui.theme.*
import kotlinx.coroutines.launch

/**
 * Ask Jomi: the user's saved conversations (title, last message, date), New chat,
 * rename / delete. Opening one continues it in SessionChatScreen (contract §6).
 */
@Composable
fun AskJomiScreen(
    onBack: () -> Unit,
    onOpenConversation: (Map<String, Any?>) -> Unit,
    onNewChat: () -> Unit
) {
    val context = LocalContext.current
    val api = remember { ISFApiClient(context) }
    val scope = rememberCoroutineScope()

    var items by remember { mutableStateOf<List<Map<String, Any?>>>(emptyList()) }
    var loading by remember { mutableStateOf(true) }
    var error by remember { mutableStateOf<String?>(null) }
    var renameTarget by remember { mutableStateOf<Map<String, Any?>?>(null) }
    var deleteTarget by remember { mutableStateOf<Map<String, Any?>?>(null) }

    fun load() {
        scope.launch {
            val res = api.listConversations()
            loading = false
            res.onSuccess { items = it; error = null }.onFailure { error = it.message }
        }
    }
    LaunchedEffect(Unit) { load() }

    SlideBackContainer(onBack = onBack) {
        Column(
            modifier = Modifier
                .fillMaxSize()
                .background(MaterialTheme.colorScheme.background)
                .padding(horizontal = 16.dp, vertical = 12.dp)
        ) {
            BackHeader(title = "Ask Jomi", subtitle = "Your saved conversations", onBack = onBack) {
                SmallIconButton(R.drawable.ic_refresh, "Refresh", { load() })
            }
            Spacer(Modifier.height(12.dp))
            Button(
                onClick = onNewChat,
                shape = RoundedCornerShape(14.dp),
                colors = ButtonDefaults.buttonColors(containerColor = JomiGreen, contentColor = Color.White),
                modifier = Modifier.fillMaxWidth().height(50.dp)
            ) {
                Icon(painterResource(R.drawable.ic_add), null, modifier = Modifier.size(20.dp))
                Spacer(Modifier.width(8.dp))
                Text("New chat", fontWeight = FontWeight.Bold, fontSize = 15.sp)
            }
            Spacer(Modifier.height(12.dp))

            when {
                loading -> Box(Modifier.fillMaxWidth().weight(1f), contentAlignment = Alignment.Center) {
                    CircularProgressIndicator(color = JomiGreen)
                }
                error != null && items.isEmpty() -> Column(Modifier.fillMaxWidth().padding(top = 24.dp), horizontalAlignment = Alignment.CenterHorizontally) {
                    Text("Could not load conversations: $error", color = JomiTextMuted, fontSize = 13.sp)
                    TextButton(onClick = { loading = true; load() }) { Text("Try again", color = JomiTeal) }
                }
                items.isEmpty() -> Column(Modifier.fillMaxWidth().padding(top = 32.dp), horizontalAlignment = Alignment.CenterHorizontally) {
                    Icon(painterResource(R.drawable.ic_sparkle), null, tint = JomiTeal, modifier = Modifier.size(40.dp))
                    Spacer(Modifier.height(10.dp))
                    Text("No conversations yet", fontWeight = FontWeight.Bold)
                    Text("Ask about your sessions, children's progress, or anything recorded.", color = JomiTextMuted, fontSize = 13.sp)
                }
                else -> LazyColumn(verticalArrangement = Arrangement.spacedBy(10.dp), modifier = Modifier.weight(1f)) {
                    items(items, key = { it["id"]?.toString() ?: it.hashCode().toString() }) { conv ->
                        ConversationRow(
                            conv = conv,
                            onOpen = { onOpenConversation(conv) },
                            onRename = { renameTarget = conv },
                            onDelete = { deleteTarget = conv }
                        )
                    }
                }
            }
        }
    }

    renameTarget?.let { conv ->
        EditTitleDialog(
            title = "Rename conversation",
            initial = conversationTitle(conv),
            onDismiss = { renameTarget = null },
            onSave = { t ->
                val res = api.renameConversation(conv["id"].toString(), t)
                if (res.isSuccess) { load(); null } else res.exceptionOrNull()?.message ?: "Could not rename"
            }
        )
    }
    deleteTarget?.let { conv ->
        AlertDialog(
            onDismissRequest = { deleteTarget = null },
            title = { Text("Delete this conversation?", fontWeight = FontWeight.Bold) },
            text = { Text("\"${conversationTitle(conv)}\" and all its messages will be deleted. Recordings and reports are not affected.") },
            confirmButton = {
                TextButton(onClick = {
                    val id = conv["id"].toString()
                    deleteTarget = null
                    scope.launch {
                        api.deleteConversation(id)
                            .onSuccess { Toast.makeText(context, "Conversation deleted", Toast.LENGTH_SHORT).show(); load() }
                            .onFailure { Toast.makeText(context, it.message ?: "Could not delete", Toast.LENGTH_LONG).show() }
                    }
                }) { Text("Delete", color = RecordRed) }
            },
            dismissButton = { TextButton(onClick = { deleteTarget = null }) { Text("Cancel") } }
        )
    }
}

private fun conversationTitle(conv: Map<String, Any?>): String =
    conv["title"]?.toString()?.takeIf { it.isNotBlank() && it != "null" } ?: "New conversation"

@Composable
private fun ConversationRow(
    conv: Map<String, Any?>,
    onOpen: () -> Unit,
    onRename: () -> Unit,
    onDelete: () -> Unit
) {
    var menu by remember { mutableStateOf(false) }
    val aboutRecording = conv["recording_id"]?.toString()?.takeIf { it.isNotBlank() && it != "null" } != null
    JomiCard(modifier = Modifier.clickable(onClick = onOpen), padding = 14.dp) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Column(Modifier.weight(1f)) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Text(
                        conversationTitle(conv),
                        fontWeight = FontWeight.SemiBold,
                        fontSize = 15.sp,
                        maxLines = 1,
                        overflow = TextOverflow.Ellipsis,
                        modifier = Modifier.weight(1f, fill = false)
                    )
                    if (aboutRecording) {
                        Spacer(Modifier.width(6.dp))
                        StatusPill(conv["recording_title"]?.toString()?.takeIf { it.isNotBlank() } ?: "Recording", JomiTeal)
                    }
                }
                val last = conv["last_message"]?.toString()?.replace("\n", " ")?.takeIf { it.isNotBlank() && it != "null" }
                if (last != null) {
                    Text(last, color = JomiTextMuted, fontSize = 13.sp, maxLines = 1, overflow = TextOverflow.Ellipsis)
                }
                val meta = listOfNotNull(
                    shortWhen(conv["updated_at"] ?: conv["created_at"]).takeIf { it.isNotBlank() },
                    (conv["message_count"] as? Number)?.toInt()?.let { "$it messages" }
                )
                if (meta.isNotEmpty()) Text(meta.joinToString(" · "), color = JomiTextMuted, fontSize = 11.sp)
            }
            Box {
                SmallIconButton(R.drawable.ic_more_vert, "More", { menu = true })
                DropdownMenu(expanded = menu, onDismissRequest = { menu = false }) {
                    DropdownMenuItem(
                        leadingIcon = { Icon(painterResource(R.drawable.ic_edit), null, modifier = Modifier.size(18.dp)) },
                        text = { Text("Rename") },
                        onClick = { menu = false; onRename() }
                    )
                    DropdownMenuItem(
                        leadingIcon = { Icon(painterResource(R.drawable.ic_delete), null, tint = RecordRed, modifier = Modifier.size(18.dp)) },
                        text = { Text("Delete", color = RecordRed) },
                        onClick = { menu = false; onDelete() }
                    )
                }
            }
        }
    }
}
