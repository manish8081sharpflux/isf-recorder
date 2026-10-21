package com.isf.recorder.ui.screens

import android.widget.Toast
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
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
 * The one coach report as a popup (round 2): markdown text, tap on the report text to edit, Save,
 * Copy, Share, Ask AI to edit (proposal → Apply / Discard). Admins also get a link to the
 * full report / transcript screens.
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun ReportSheet(
    recordingId: String,
    title: String,
    onDismiss: () -> Unit,
    onOpenFull: (() -> Unit)? = null,
    onOpenTranscript: (() -> Unit)? = null
) {
    val context = LocalContext.current
    val api = remember { ISFApiClient(context) }
    val scope = rememberCoroutineScope()
    val sheetState = rememberModalBottomSheetState(skipPartiallyExpanded = true)
    val audio = rememberLazyAudio(api.audioUrl(recordingId))   // released (stops) when the sheet closes

    var report by remember { mutableStateOf<Map<String, Any?>?>(null) }
    var loading by remember { mutableStateOf(true) }
    var error by remember { mutableStateOf<String?>(null) }
    // saved: an unfinished edit comes back after switching apps (also if Android closed Jomi meanwhile)
    var editing by rememberSaveable { mutableStateOf(false) }
    var text by rememberSaveable { mutableStateOf("") }
    var busy by remember { mutableStateOf<String?>(null) }
    var showAi by remember { mutableStateOf(false) }
    var proposal by remember { mutableStateOf<String?>(null) }

    val status = report?.get("status")?.toString()
    val content = report?.get("content")?.toString().orEmpty()
    val canEdit = report != null && (report?.get("can_edit") as? Boolean ?: true) && status != "generating"
    val tr = rememberReportTranslation(api, recordingId, content)   // English | हिंदी

    suspend fun load() {
        api.getReport(recordingId)
            .onSuccess { report = it; error = null; if (!editing) text = it?.get("content")?.toString().orEmpty() }
            .onFailure { error = it.message }
        loading = false
    }
    LaunchedEffect(recordingId) { load() }
    // who / where / when, shown above the report and put at the top of Copy/Share
    var details by remember(recordingId) { mutableStateOf<List<String>>(emptyList()) }
    var detail by remember(recordingId) { mutableStateOf<Map<String, Any?>?>(null) }
    LaunchedEffect(recordingId) { api.getRecordingDetail(recordingId).onSuccess { detail = it; details = reportDetailsLines(it) } }
    // Calls (14 Oct): private to the owner; tasks + "Share with admin" in the popup
    val isCall = detail?.get("source")?.toString() == "call"
    val isOwner = detail?.get("user_id")?.toString() == com.isf.recorder.data.prefs.UserPreferences(context).userId
    var sharedWithAdmin by remember(recordingId) { mutableStateOf(false) }
    LaunchedEffect(detail) { sharedWithAdmin = detail?.get("shared_with_admin") as? Boolean ?: false }
    LaunchedEffect(status) {
        while (status == "generating") { delay(5_000); load() }
    }

    fun save(newText: String, ok: String) {
        busy = "save"
        scope.launch {
            val res = api.saveReport(recordingId, newText)
            busy = null
            res.onSuccess { editing = false; Toast.makeText(context, ok, Toast.LENGTH_SHORT).show(); load() }
                .onFailure { Toast.makeText(context, it.message ?: "Could not save", Toast.LENGTH_LONG).show() }
        }
    }

    val sheetScroll = rememberScrollState()
    val shareBody = {
        val head = (listOf(title) + details).joinToString("\n")
        "$head\n\n${if (editing) text else tr.shown(content).ifBlank { content }}"
    }

    ModalBottomSheet(onDismissRequest = onDismiss, sheetState = sheetState, containerColor = MaterialTheme.colorScheme.background) {
        val editMode = editing && report != null && status != "generating"
        Column(
            Modifier.fillMaxWidth()
                .then(if (editMode) Modifier.fillMaxHeight().imePadding() else Modifier)
                .navigationBarsPadding()   // Copy / Share stay above the phone's ◁ ○ ☰ buttons
                .padding(horizontal = 16.dp).padding(bottom = if (editMode) 12.dp else 24.dp)
        ) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Column(Modifier.weight(1f)) {
                    Text("Report", color = JomiTeal, fontSize = 12.sp, fontWeight = FontWeight.Bold)
                    Text(title, fontWeight = FontWeight.Bold, fontSize = 17.sp, maxLines = 2, overflow = TextOverflow.Ellipsis)
                    if (!editMode) details.forEach {
                        Text(it, color = JomiTextMuted, fontSize = 12.sp, maxLines = 2, overflow = TextOverflow.Ellipsis)
                    }
                }
                if (status == "final") StatusPill("Final", JomiTeal)
            }
            Spacer(Modifier.height(8.dp))
            if (!editMode && report != null && status != "generating" && content.isNotBlank()) {
                ReportLanguageBar(tr)
                Spacer(Modifier.height(8.dp))
            }
            if (!editMode) MiniAudioBar(audio, title = title, fallbackDurationSec = 0)
            else MiniAudioBar(audio, title = title, fallbackDurationSec = 0)   // listen while typing
            Spacer(Modifier.height(10.dp))

            if (editMode) {
                // Edit mode (round 3): only a full-height editor + Save / Cancel, so most text is visible
                OutlinedTextField(
                    value = text,
                    onValueChange = { text = it },
                    textStyle = LocalTextStyle.current.copy(fontSize = 14.sp, lineHeight = 20.sp),
                    colors = OutlinedTextFieldDefaults.colors(focusedBorderColor = JomiGreen, unfocusedBorderColor = JomiBorder),
                    modifier = Modifier.fillMaxWidth().weight(1f)
                )
                Spacer(Modifier.height(10.dp))
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    Button(
                        onClick = { audio.pause(); save(text, "Report saved") },
                        enabled = busy == null && text != content,
                        colors = ButtonDefaults.buttonColors(containerColor = JomiGreen),
                        modifier = Modifier.weight(1f)
                    ) { Text(if (busy == "save") "Saving…" else "Save") }
                    OutlinedButton(onClick = { audio.pause(); editing = false; text = content }, enabled = busy == null, modifier = Modifier.weight(1f)) { Text("Cancel") }
                }
            } else Box(Modifier.weight(1f, fill = false)) {
              Column(Modifier.padding(end = 14.dp).verticalScroll(sheetScroll)) {   // end: room for the scroll bar
                when {
                    loading -> Box(Modifier.fillMaxWidth().padding(32.dp), contentAlignment = Alignment.Center) {
                        CircularProgressIndicator(color = JomiGreen)
                    }
                    report == null -> JomiCard {
                        Text(error?.let { "Could not load the report: $it" } ?: "No report yet.", color = JomiTextMuted, fontSize = 14.sp)
                        if (error == null) {
                            Spacer(Modifier.height(8.dp))
                            Button(
                                enabled = busy == null,
                                colors = ButtonDefaults.buttonColors(containerColor = JomiGreen),
                                onClick = {
                                    busy = "gen"
                                    scope.launch {
                                        val r = api.regenerateReport(recordingId)
                                        busy = null
                                        r.onSuccess { load() }.onFailure { Toast.makeText(context, it.message ?: "Could not start", Toast.LENGTH_LONG).show() }
                                    }
                                }
                            ) { Text("Write the report now") }
                        }
                    }
                    status == "generating" -> TranscriptionStatusCard(TranscriptionUi.WritingReport, {}, {})
                    else -> JomiCard {
                        if (status == "error") {
                            MessageBanner(report?.get("error_message")?.toString() ?: "The report could not be written.", color = RecordRed)
                            Spacer(Modifier.height(8.dp))
                        }
                        if (!tr.isEnglish && tr.shown(content).isBlank()) TranslationStatus(tr)
                        else Box(Modifier.fillMaxWidth().clickable(enabled = canEdit && tr.isEnglish) { text = content; editing = true }) {
                            if (content.isBlank()) Text("The report is empty. Tap to write it.", color = JomiTextMuted)
                            else MarkdownText(tr.shown(content))
                        }
                        if (canEdit) {
                            Spacer(Modifier.height(6.dp))
                            Text(if (tr.isEnglish) "Tap on the report text to edit" else "Translated by AI — choose English to edit",
                                color = JomiTextMuted, fontSize = 11.sp)
                        }
                    }
                }
                if (isCall) {
                    Spacer(Modifier.height(10.dp))
                    JomiCard {
                        Text("✅ Tasks from this call", fontWeight = FontWeight.Bold, fontSize = 15.sp)
                        if (isOwner) TasksPanel(api, recordingId)
                        else Text("Only the person who made the call can change its tasks.", color = JomiTextMuted, fontSize = 12.sp)
                    }
                    if (isOwner) {
                        Spacer(Modifier.height(10.dp))
                        Row(verticalAlignment = Alignment.CenterVertically) {
                            Column(Modifier.weight(1f)) {
                                Text("Share with admin", fontWeight = FontWeight.SemiBold, fontSize = 14.sp)
                                Text(if (sharedWithAdmin) "Admins can see this call and its summary" else "Only you can see this call",
                                    color = JomiTextMuted, fontSize = 12.sp)
                            }
                            Switch(checked = sharedWithAdmin, onCheckedChange = { on ->
                                scope.launch {
                                    api.shareCall(recordingId, on).onSuccess { sharedWithAdmin = on }
                                        .onFailure { Toast.makeText(context, it.message ?: "Could not change sharing", Toast.LENGTH_SHORT).show() }
                                }
                            }, colors = SwitchDefaults.colors(checkedTrackColor = JomiGreen))
                        }
                    }
                }
              }
              ScrollbarFor(sheetScroll)
            }

            if (!editMode && report != null && status != "generating") {
                Spacer(Modifier.height(12.dp))
                if (editing) {
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        Button(
                            onClick = { save(text, "Report saved") },
                            enabled = busy == null && text != content,
                            colors = ButtonDefaults.buttonColors(containerColor = JomiGreen),
                            modifier = Modifier.weight(1f)
                        ) { Text(if (busy == "save") "Saving…" else "Save") }
                        OutlinedButton(onClick = { editing = false; text = content }, enabled = busy == null, modifier = Modifier.weight(1f)) { Text("Cancel") }
                    }
                    Spacer(Modifier.height(8.dp))
                }
                if (canEdit) {
                    ActionChipButton("Ask AI to edit", R.drawable.ic_sparkle, { showAi = true },
                        enabled = busy == null, modifier = Modifier.fillMaxWidth())
                    Spacer(Modifier.height(8.dp))
                }
                Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                    val has = (if (editing) text else content).isNotBlank()
                    ActionChipButton("Copy", R.drawable.ic_copy, { copyOrShareText(context, title, shareBody(), share = false) },
                        enabled = has, modifier = Modifier.weight(1f))
                    ActionChipButton("Share", R.drawable.ic_share, {
                        // Unsaved edits or a call: share the text itself; otherwise a link to the full report
                        if (isCall || (editing && text != content)) copyOrShareText(context, title, shareBody(), share = true)
                        else scope.launch { shareReportLink(context, api, recordingId, title, details, tr.lang) { shareBody() } }
                    },
                        enabled = has, modifier = Modifier.weight(1f))
                }
            }
            if (onOpenTranscript != null && !editMode) {
                TextButton(onClick = onOpenTranscript, modifier = Modifier.align(Alignment.CenterHorizontally)) {
                    Text("View transcript", color = JomiTeal, fontWeight = FontWeight.SemiBold)
                }
            }
            if (onOpenFull != null && !editMode) {
                TextButton(onClick = onOpenFull, modifier = Modifier.align(Alignment.CenterHorizontally)) {
                    Text("Open full report, transcript & versions", color = JomiTeal)
                }
            }
        }
    }

    if (showAi) {
        AiEditDialog(
            busy = busy == "ai",
            onDismiss = { if (busy != "ai") showAi = false },
            onSubmit = { instruction ->
                busy = "ai"
                scope.launch {
                    val res = api.aiEditReport(recordingId, instruction, if (editing) text else content)
                    busy = null
                    res.onSuccess { showAi = false; proposal = it }
                        .onFailure { Toast.makeText(context, it.message ?: "AI edit failed", Toast.LENGTH_LONG).show() }
                }
            }
        )
    }
    proposal?.let { p ->
        AlertDialog(
            onDismissRequest = { proposal = null },
            title = { Text("Proposed report", fontWeight = FontWeight.Bold) },
            text = {
                Column(Modifier.heightIn(max = 420.dp).verticalScroll(rememberScrollState())) {
                    Text("Nothing is saved until you tap Apply.", color = JomiTextMuted, fontSize = 12.sp)
                    Spacer(Modifier.height(8.dp))
                    MarkdownText(p)
                }
            },
            confirmButton = {
                Button(onClick = { proposal = null; text = p; save(p, "Applied and saved") }, colors = ButtonDefaults.buttonColors(containerColor = JomiGreen)) { Text("Apply") }
            },
            dismissButton = { TextButton(onClick = { proposal = null }) { Text("Discard") } }
        )
    }
}
