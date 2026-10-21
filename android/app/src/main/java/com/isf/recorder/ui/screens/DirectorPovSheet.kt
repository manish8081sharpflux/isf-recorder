package com.isf.recorder.ui.screens

import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.isf.recorder.R
import com.isf.recorder.data.api.ISFApiClient
import com.isf.recorder.ui.components.*
import com.isf.recorder.ui.theme.*
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch

/**
 * Director's POV (21 Oct), ADMINS ONLY: one tap from Previous recordings. The first open makes it from the org's
 * "Director…" AI chat template (same answer as picking that template in AI chat) and the server saves it, so
 * later opens are instant. Regenerate makes a fresh one. Copy only: no share link, so it stays with admins.
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun DirectorPovSheet(recordingId: String, title: String, onDismiss: () -> Unit) {
    val context = LocalContext.current
    val api = remember { ISFApiClient(context) }
    val scope = rememberCoroutineScope()
    val sheetState = rememberModalBottomSheetState(skipPartiallyExpanded = true)

    var pov by remember { mutableStateOf<Map<String, Any?>?>(null) }
    var error by remember { mutableStateOf<String?>(null) }
    var confirmRegenerate by remember { mutableStateOf(false) }
    val status = pov?.get("status")?.toString()
    val content = pov?.get("content")?.toString().orEmpty()

    suspend fun make(regenerate: Boolean) {
        api.makeDirectorPov(recordingId, regenerate).onSuccess { pov = it; error = null }.onFailure { error = it.message }
    }
    LaunchedEffect(recordingId) {
        api.getDirectorPov(recordingId)
            .onSuccess { pov = it; error = null; if (it["status"] == "none") make(regenerate = false) }
            .onFailure { error = it.message }
    }
    LaunchedEffect(status) {
        while (status == "generating") {
            delay(3_000)
            api.getDirectorPov(recordingId).onSuccess { pov = it }
        }
    }

    ModalBottomSheet(onDismissRequest = onDismiss, sheetState = sheetState, containerColor = MaterialTheme.colorScheme.background) {
        Column(Modifier.fillMaxWidth().padding(horizontal = 16.dp).padding(bottom = 24.dp)) {
            Text("🎬 Director's POV", color = JomiTeal, fontSize = 12.sp, fontWeight = FontWeight.Bold)
            Text(title, fontWeight = FontWeight.Bold, fontSize = 17.sp, maxLines = 2, overflow = TextOverflow.Ellipsis)
            Text("Only admins can see this.", color = JomiTextMuted, fontSize = 12.sp)
            Spacer(Modifier.height(10.dp))

            when {
                error != null -> Text("⚠️ $error", color = RecordRed, fontSize = 14.sp)
                pov == null || status == "none" || status == "generating" -> Row(verticalAlignment = Alignment.CenterVertically) {
                    CircularProgressIndicator(Modifier.size(18.dp), color = JomiGreen, strokeWidth = 2.dp)
                    Spacer(Modifier.width(10.dp))
                    Text("Writing the Director's POV… this takes about a minute.", fontSize = 14.sp)
                }
                status == "error" -> Text("⚠️ ${pov?.get("error_message") ?: "It could not be made."}", color = RecordRed, fontSize = 14.sp)
                else -> Column(Modifier.heightIn(max = 520.dp).verticalScroll(rememberScrollState())) {
                    MarkdownText(content)
                    val by = pov?.get("generated_by_name")?.toString()?.takeIf { it.isNotBlank() && it != "null" }
                    val at = shortWhen(pov?.get("generated_at"))
                    if (by != null || at.isNotBlank()) {
                        Spacer(Modifier.height(8.dp))
                        Text(listOfNotNull(by?.let { "Made by $it" }, at.takeIf { it.isNotBlank() }).joinToString(" · "),
                            color = JomiTextMuted, fontSize = 12.sp)
                    }
                }
            }

            Spacer(Modifier.height(14.dp))
            Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                ActionChipButton("Copy", R.drawable.ic_copy,
                    { copyOrShareText(context, "Director's POV — $title", "*Director's POV — $title*\n\n$content", share = false, toast = "Director's POV copied") },
                    enabled = status == "ready" && content.isNotBlank(), modifier = Modifier.weight(1f))
                ActionChipButton("Regenerate", R.drawable.ic_refresh, { confirmRegenerate = true },
                    enabled = status == "ready" || status == "error" || error != null, modifier = Modifier.weight(1f))
            }
        }
    }

    if (confirmRegenerate) {
        AlertDialog(
            onDismissRequest = { confirmRegenerate = false },
            title = { Text("Make a new Director's POV?") },
            text = { Text("The AI writes it again from the transcript. The current one is replaced.") },
            confirmButton = {
                Button(onClick = {
                    confirmRegenerate = false
                    pov = mapOf("status" to "generating")
                    scope.launch { make(regenerate = true) }
                }, colors = ButtonDefaults.buttonColors(containerColor = JomiGreen)) { Text("Regenerate") }
            },
            dismissButton = { TextButton(onClick = { confirmRegenerate = false }) { Text("Cancel") } }
        )
    }
}
