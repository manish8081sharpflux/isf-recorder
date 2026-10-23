package com.isf.recorder.ui.screens

import androidx.compose.ui.text.style.TextOverflow

import androidx.compose.foundation.clickable

import android.widget.Toast
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.background
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.selection.selectable
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.isf.recorder.R
import com.isf.recorder.data.api.ISFApiClient
import com.isf.recorder.data.prefs.UserPreferences
import com.isf.recorder.ui.components.*
import com.isf.recorder.ui.theme.*
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch

/**
 * Report for one recording (contract §5): audio pinned on top, the report (draft editable),
 * Save / Finalize / Regenerate (template picker) / admin Reopen, "Ask AI to edit" with
 * preview → Apply / Discard, Copy / Share, and a link to the transcript screen.
 */
@Composable
fun ReportScreen(
    recordingId: String,
    onBack: () -> Unit,
    onViewTranscript: (String) -> Unit,
    onOpenChat: (recordingId: String, title: String) -> Unit
) {
    val context = LocalContext.current
    val api = remember { ISFApiClient(context) }
    val prefs = remember { UserPreferences(context) }
    val scope = rememberCoroutineScope()

    var detail by remember { mutableStateOf<Map<String, Any?>?>(null) }
    var report by remember { mutableStateOf<Map<String, Any?>?>(null) }
    var loading by remember { mutableStateOf(true) }
    var loadError by remember { mutableStateOf<String?>(null) }

    // saved: an unfinished edit comes back after switching apps (also if Android closed Jomi meanwhile)
    var editing by rememberSaveable { mutableStateOf(false) }
    var editText by rememberSaveable { mutableStateOf("") }
    var busy by remember { mutableStateOf<String?>(null) } // "save" | "finalize" | "regenerate" | "reopen" | "ai"

    var showRegenerate by remember { mutableStateOf(false) }
    var showAiAsk by remember { mutableStateOf(false) }
    var aiProposal by remember { mutableStateOf<String?>(null) }
    var confirmDiscardBack by remember { mutableStateOf(false) }

    val reportStatus = report?.get("status")?.toString()
    val content = report?.get("content")?.toString().orEmpty()
    val dirty = editing && editText != content
    val recordingStatus = detail?.get("status")?.toString()
    val title = detail?.let { cleanTitle(it) } ?: "Report"

    suspend fun reload(showSpinner: Boolean = false) {
        if (showSpinner) loading = true
        val d = api.getRecordingDetail(recordingId)
        val r = api.getReport(recordingId)
        loading = false
        d.onSuccess { detail = it }
        r.onSuccess { report = it; loadError = null }.onFailure { loadError = it.message }
        if (d.isFailure && r.isFailure) loadError = d.exceptionOrNull()?.message
    }

    LaunchedEffect(recordingId) { reload(showSpinner = true) }
    // Poll while the transcript or the report is still being produced.
    val waiting = reportStatus == "generating" || (report == null && recordingStatus in listOf("uploading", "processing", "recording"))
    LaunchedEffect(waiting) {
        while (waiting) {
            delay(5_000)
            reload()
        }
    }

    fun act(kind: String, block: suspend () -> Result<*>, ok: String) {
        busy = kind
        scope.launch {
            val res = block()
            busy = null
            if (res.isSuccess) {
                Toast.makeText(context, ok, Toast.LENGTH_SHORT).show()
                reload()
            } else {
                Toast.makeText(context, res.exceptionOrNull()?.message ?: "Something went wrong", Toast.LENGTH_LONG).show()
            }
        }
    }

    val tr = rememberReportTranslation(api, recordingId, content)   // English | हिंदी

    fun shareBody(): String = buildString {
        append(title)
        reportDetailsLines(detail).forEach { append("\n").append(it) }
        append("\n\n").append(if (editing) editText else tr.shown(content).ifBlank { content })
    }

    val back = { if (dirty) confirmDiscardBack = true else onBack() }
    val audio = rememberLazyAudio(api.audioUrl(recordingId), audioWasCleanedUp(detail))
    LaunchedEffect(editing) { if (!editing) audio.pause() }   // Save / Cancel / Finalize end editing -> pause

    SlideBackContainer(onBack = back) {
        Column(
            modifier = Modifier
                .fillMaxSize()
                .background(MaterialTheme.colorScheme.background)
                .imePadding()
                .padding(horizontal = 16.dp, vertical = 12.dp)
        ) {
            BackHeader(
                title = title,
                subtitle = detail?.let { d ->
                    listOfNotNull(recordingDate(d).takeIf { it.isNotBlank() }, modeLabel(d["mode"]), d["balgruh_name"]?.toString()?.takeIf { it.isNotBlank() })
                        .joinToString(" · ")
                },
                onBack = back
            ) {
                SmallIconButton(R.drawable.ic_chat, "AI chat about this recording", { onOpenChat(recordingId, title) }, tint = JomiTeal)
                SmallIconButton(R.drawable.ic_refresh, "Refresh", { scope.launch { reload() } })
            }
            // Recorded by / assistant coaches (or participants): from the recording, not the AI text
            if (!editing) reportDetailsLines(detail).drop(1).forEach {
                Text(it, color = JomiTextMuted, fontSize = 12.sp, maxLines = 2, overflow = TextOverflow.Ellipsis,
                    modifier = Modifier.padding(horizontal = 16.dp))
            }
            Spacer(Modifier.height(10.dp))

            // ── Audio: optional, collapsed until "▶ Play audio"; a mini bar while editing (v2.1) ──
            val hasAudio = detail?.get("has_audio") as? Boolean ?: false
            if (detail != null) {
                if (hasAudio) {
                    if (editing) MiniAudioBar(audio, title = title, fallbackDurationSec = recordingSeconds(detail!!))
                    else MiniAudioBar(audio, title = title, fallbackDurationSec = recordingSeconds(detail!!))
                } else {
                    Text(
                        if (recordingStatus in listOf("uploading", "recording")) "Audio is still uploading…" else audioUnavailableMessage(audioWasCleanedUp(detail)),
                        color = JomiTextMuted, fontSize = 12.sp
                    )
                }
                Spacer(Modifier.height(10.dp))
            }

            // ── Report body (scrolls under the pinned player) ──────────────────
            val bodyScroll = rememberScrollState()
            Box(Modifier.weight(1f)) {
            Column(modifier = Modifier.fillMaxSize().padding(end = 14.dp).verticalScroll(bodyScroll)) {   // end: room for the scroll bar
                when {
                    loading -> Box(Modifier.fillMaxWidth().padding(40.dp), contentAlignment = Alignment.Center) {
                        CircularProgressIndicator(color = JomiGreen)
                    }
                    report == null -> NoReportCard(
                        recordingStatus = recordingStatus,
                        error = loadError,
                        busy = busy == "regenerate",
                        onGenerate = { showRegenerate = true }
                    )
                    reportStatus == "generating" -> TranscriptionStatusCard(TranscriptionUi.WritingReport, onRetry = {}, onOpenReport = {})
                    else -> {
                        ReportMetaRow(report!!)
                        if (reportStatus == "error") {
                            Spacer(Modifier.height(8.dp))
                            MessageBanner(report?.get("error_message")?.toString() ?: "The report could not be written.", color = RecordRed)
                        }
                        Spacer(Modifier.height(10.dp))
                        if (!editing && content.isNotBlank()) {
                            ReportLanguageBar(tr)
                            Spacer(Modifier.height(10.dp))
                        }
                        JomiCard {
                            if (editing) {
                                OutlinedTextField(
                                    value = editText,
                                    onValueChange = { editText = it },
                                    minLines = 12,
                                    textStyle = LocalTextStyle.current.copy(fontSize = 14.sp, lineHeight = 20.sp),
                                    colors = OutlinedTextFieldDefaults.colors(focusedBorderColor = JomiGreen, unfocusedBorderColor = JomiBorder),
                                    modifier = Modifier.fillMaxWidth()
                                )
                            } else if (content.isBlank()) {
                                Text("The report is empty.", color = JomiTextMuted)
                            } else {
                                // Tap the report to edit it (same as the popup); the old text-selection
                                // wrapper swallowed the tap, so only the Edit button worked.
                                val st = report?.get("status")?.toString()
                                val tapEdit = (report?.get("can_edit") as? Boolean ?: true) &&
                                    (st == "draft" || st == "error" || (st == "final" && prefs.isAdmin))
                                if (!tr.isEnglish && tr.shown(content).isBlank()) TranslationStatus(tr)
                                else Column(Modifier.fillMaxWidth().clickable(enabled = tapEdit && tr.isEnglish) { editText = content; editing = true }) {
                                    MarkdownText(tr.shown(content))
                                    if (tapEdit) {
                                        Spacer(Modifier.height(6.dp))
                                        Text(if (tr.isEnglish) "Tap on the report text to edit" else "Translated by AI — choose English to edit",
                                            color = JomiTextMuted, fontSize = 12.sp)
                                    }
                                }
                            }
                        }
                    }
                }

                Spacer(Modifier.height(12.dp))
                ReportActions(
                    report = report,
                    editing = editing,
                    dirty = dirty,
                    busy = busy,
                    isAdmin = prefs.isAdmin,
                    onEdit = { editText = content; editing = true },
                    onCancelEdit = { editing = false },
                    onSave = {
                        act("save", { api.saveReport(recordingId, editText).onSuccess { editing = false } }, "Draft saved")
                    },
                    onFinalize = {
                        act("finalize", {
                            if (dirty) api.saveReport(recordingId, editText).onFailure { return@act Result.failure<Any>(it) }
                            api.finalizeReport(recordingId).onSuccess { editing = false }
                        }, "Report finalized")
                    },
                    onReopen = { act("reopen", { api.reopenReport(recordingId) }, "Report reopened as draft") },
                    onRegenerate = { showRegenerate = true },
                    onAskAi = { showAiAsk = true },
                    onCopy = { copyOrShareText(context, title, shareBody(), share = false) },
                    onShare = {
                        // Unsaved edits or a call: share the text itself; otherwise a link to the full report
                        if (dirty || detail?.get("source")?.toString() == "call") copyOrShareText(context, title, shareBody(), share = true)
                        else scope.launch {
                            shareReportLink(context, api, recordingId, title, reportDetailsLines(detail), tr.lang) { shareBody() }
                        }
                    }
                )
                Spacer(Modifier.height(4.dp))
                TextButton(onClick = { onViewTranscript(recordingId) }, modifier = Modifier.align(Alignment.CenterHorizontally)) {
                    Text("View transcript", color = JomiTeal, fontWeight = FontWeight.SemiBold)
                }
                Spacer(Modifier.height(24.dp))
            }
            ScrollbarFor(bodyScroll)
            }
        }
    }

    if (showRegenerate) {
        RegenerateDialog(
            api = api,
            currentTemplateId = report?.get("template_id")?.toString(),
            replacesText = report != null && content.isNotBlank(),
            onDismiss = { showRegenerate = false },
            onConfirm = { templateId ->
                showRegenerate = false
                editing = false
                act("regenerate", { api.regenerateReport(recordingId, templateId) }, "Writing a new report…")
            }
        )
    }

    if (showAiAsk) {
        AiEditDialog(
            busy = busy == "ai",
            onDismiss = { if (busy != "ai") showAiAsk = false },
            onSubmit = { instruction ->
                busy = "ai"
                scope.launch {
                    val res = api.aiEditReport(recordingId, instruction, if (editing) editText else content)
                    busy = null
                    res.onSuccess { showAiAsk = false; aiProposal = it }
                        .onFailure { Toast.makeText(context, it.message ?: "AI edit failed", Toast.LENGTH_LONG).show() }
                }
            }
        )
    }

    aiProposal?.let { proposed ->
        AlertDialog(
            onDismissRequest = { aiProposal = null },
            title = { Text("Proposed report", fontWeight = FontWeight.Bold) },
            text = {
                Column(Modifier.heightIn(max = 420.dp).verticalScroll(rememberScrollState())) {
                    Text("Nothing is saved until you tap Apply.", color = JomiTextMuted, fontSize = 12.sp)
                    Spacer(Modifier.height(8.dp))
                    MarkdownText(proposed)
                }
            },
            confirmButton = {
                Button(
                    onClick = {
                        aiProposal = null
                        act("save", { api.saveReport(recordingId, proposed).onSuccess { editing = false } }, "Applied and saved")
                    },
                    colors = ButtonDefaults.buttonColors(containerColor = JomiGreen)
                ) { Text("Apply") }
            },
            dismissButton = { TextButton(onClick = { aiProposal = null }) { Text("Discard") } }
        )
    }

    if (confirmDiscardBack) {
        AlertDialog(
            onDismissRequest = { confirmDiscardBack = false },
            title = { Text("Discard your edits?") },
            text = { Text("You have unsaved changes to this report.") },
            confirmButton = { TextButton(onClick = { confirmDiscardBack = false; onBack() }) { Text("Discard", color = RecordRed) } },
            dismissButton = { TextButton(onClick = { confirmDiscardBack = false }) { Text("Keep editing") } }
        )
    }
}

@Composable
private fun NoReportCard(recordingStatus: String?, error: String?, busy: Boolean, onGenerate: () -> Unit) {
    when (recordingStatus) {
        "recording", "uploading" -> TranscriptionStatusCard(TranscriptionUi.Saving, {}, {})
        "processing", "stitching", "pending" -> TranscriptionStatusCard(TranscriptionUi.Transcribing, {}, {})
        else -> JomiCard {
            Text("No report yet", fontWeight = FontWeight.Bold, fontSize = 16.sp)
            Spacer(Modifier.height(4.dp))
            Text(
                error ?: if (recordingStatus == "error") "Transcription failed, so there is no report. Retry it from the transcript screen."
                else "Generate a report from the transcript with a template.",
                color = JomiTextMuted, fontSize = 13.sp
            )
            if (recordingStatus == "completed") {
                Spacer(Modifier.height(10.dp))
                Button(onClick = onGenerate, enabled = !busy, colors = ButtonDefaults.buttonColors(containerColor = JomiGreen)) {
                    Icon(painterResource(R.drawable.ic_sparkle), null, modifier = Modifier.size(16.dp))
                    Spacer(Modifier.width(6.dp))
                    Text("Generate report")
                }
            }
        }
    }
}

@Composable
private fun ReportMetaRow(report: Map<String, Any?>) {
    val status = report["status"]?.toString()
    val (label, color) = when (status) {
        "final" -> "Final" to JomiTeal
        "draft" -> "Draft" to JomiGreen
        "error" -> "Failed" to RecordRed
        else -> (status ?: "—") to JomiTextMuted
    }
    Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.fillMaxWidth()) {
        StatusPill(label, color)
        Spacer(Modifier.width(8.dp))
        val parts = listOfNotNull(
            report["template_title"]?.toString()?.takeIf { it.isNotBlank() },
            report["updated_by_name"]?.toString()?.takeIf { it.isNotBlank() }?.let { "edited by $it" },
            shortWhen(report["finalized_at"] ?: report["updated_at"] ?: report["generated_at"]).takeIf { it.isNotBlank() }
        )
        Text(parts.joinToString(" · "), color = JomiTextMuted, fontSize = 12.sp, maxLines = 1)
    }
}

@Composable
private fun ReportActions(
    report: Map<String, Any?>?,
    editing: Boolean,
    dirty: Boolean,
    busy: String?,
    isAdmin: Boolean,
    onEdit: () -> Unit,
    onCancelEdit: () -> Unit,
    onSave: () -> Unit,
    onFinalize: () -> Unit,
    onReopen: () -> Unit,
    onRegenerate: () -> Unit,
    onAskAi: () -> Unit,
    onCopy: () -> Unit,
    onShare: () -> Unit
) {
    if (report == null) return
    val status = report["status"]?.toString()
    val canEdit = (report["can_edit"] as? Boolean ?: true) && (status == "draft" || status == "error" || (status == "final" && isAdmin))
    val hasContent = report["content"]?.toString()?.isNotBlank() == true
    Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
        Row(Modifier.fillMaxWidth().horizontalScroll(rememberScrollState()), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            if (editing) {
                // always tappable (a grey Save looked broken); nothing changed -> just leave edit mode
                Button(onClick = { if (dirty) onSave() else onCancelEdit() }, enabled = busy == null, colors = ButtonDefaults.buttonColors(containerColor = JomiGreen)) {
                    Text(if (busy == "save") "Saving…" else "Save")
                }
                OutlinedButton(onClick = onCancelEdit, enabled = busy == null) { Text("Cancel") }
            } else if (canEdit && status != "generating") {
                ActionChipButton("Edit", R.drawable.ic_edit, onEdit, enabled = busy == null)
            }
            if (canEdit && status != "generating") {
                ActionChipButton("Ask AI to edit", R.drawable.ic_sparkle, onAskAi, enabled = busy == null && (hasContent || editing))
            }
            if (status == "draft") {
                ActionChipButton("Finalize", R.drawable.ic_check, onFinalize, enabled = busy == null && (hasContent || editing))
            }
            if (status == "final" && isAdmin) {
                ActionChipButton("Reopen", R.drawable.ic_restore, onReopen, enabled = busy == null)
            }
            if (status != "generating" && (status != "final" || isAdmin)) {
                ActionChipButton("Regenerate", R.drawable.ic_refresh, onRegenerate, enabled = busy == null,
                    tint = MaterialTheme.colorScheme.onBackground, container = Color.White, border = JomiBorder)
            }
        }
        Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
            ActionChipButton("Copy", R.drawable.ic_copy, onCopy, enabled = hasContent, modifier = Modifier.weight(1f))
            ActionChipButton("Share", R.drawable.ic_share, onShare, enabled = hasContent, modifier = Modifier.weight(1f))
        }
        if (busy == "finalize" || busy == "regenerate" || busy == "reopen") {
            LinearProgressIndicator(color = JomiGreen, modifier = Modifier.fillMaxWidth())
        }
    }
}

/** Template picker (from /chat/templates) for Regenerate / Generate. */
@Composable
private fun RegenerateDialog(
    api: ISFApiClient,
    currentTemplateId: String?,
    replacesText: Boolean,
    onDismiss: () -> Unit,
    onConfirm: (String?) -> Unit
) {
    var templates by remember { mutableStateOf<List<Map<String, Any?>>?>(null) }
    var picked by remember { mutableStateOf(currentTemplateId) }
    var error by remember { mutableStateOf<String?>(null) }
    LaunchedEffect(Unit) {
        api.listChatTemplates().onSuccess { templates = it }.onFailure { templates = emptyList(); error = it.message }
    }
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text(if (replacesText) "Regenerate report" else "Generate report", fontWeight = FontWeight.Bold) },
        text = {
            Column(Modifier.heightIn(max = 420.dp).verticalScroll(rememberScrollState())) {
                if (replacesText) {
                    Text("The current report text will be replaced.", color = WarningOrange, fontSize = 13.sp)
                    Spacer(Modifier.height(8.dp))
                }
                Text("Template", fontWeight = FontWeight.SemiBold, fontSize = 13.sp)
                val list = templates
                when {
                    list == null -> CircularProgressIndicator(Modifier.size(20.dp).padding(8.dp), strokeWidth = 2.dp)
                    list.isEmpty() -> Text(error ?: "No templates. The default for this mode will be used.", color = JomiTextMuted, fontSize = 13.sp)
                    else -> {
                        TemplateOption("Default for this mode", picked == null) { picked = null }
                        list.forEach { t ->
                            val id = t["id"]?.toString()
                            TemplateOption(t["title"]?.toString() ?: "Template", picked == id) { picked = id }
                        }
                    }
                }
            }
        },
        confirmButton = {
            Button(onClick = { onConfirm(picked) }, colors = ButtonDefaults.buttonColors(containerColor = JomiGreen)) {
                Text(if (replacesText) "Regenerate" else "Generate")
            }
        },
        dismissButton = { TextButton(onClick = onDismiss) { Text("Cancel") } }
    )
}

@Composable
private fun TemplateOption(label: String, selected: Boolean, onClick: () -> Unit) {
    Row(
        verticalAlignment = Alignment.CenterVertically,
        modifier = Modifier.fillMaxWidth().selectable(selected = selected, onClick = onClick).padding(vertical = 4.dp)
    ) {
        RadioButton(selected = selected, onClick = onClick, colors = RadioButtonDefaults.colors(selectedColor = JomiGreen))
        Text(label, fontSize = 14.sp)
    }
}

@Composable
internal fun AiEditDialog(busy: Boolean, onDismiss: () -> Unit, onSubmit: (String) -> Unit) {
    var instruction by remember { mutableStateOf("") }
    AlertDialog(
        onDismissRequest = onDismiss,
        icon = { Icon(painterResource(R.drawable.ic_sparkle), null, tint = JomiTeal) },
        title = { Text("Ask AI to edit", fontWeight = FontWeight.Bold) },
        text = {
            Column {
                Text("Say what to change. You will see the new version before anything is saved.", fontSize = 13.sp, color = JomiTextMuted)
                Spacer(Modifier.height(8.dp))
                Row(Modifier.horizontalScroll(rememberScrollState()), horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                    listOf("Make it shorter", "Add more detail", "Fix spelling and grammar", "Use simpler words").forEach { s ->
                        SuggestionChip(onClick = { instruction = s }, label = { Text(s, fontSize = 12.sp) })
                    }
                }
                Spacer(Modifier.height(8.dp))
                OutlinedTextField(
                    value = instruction,
                    onValueChange = { instruction = it },
                    minLines = 3,
                    enabled = !busy,
                    placeholder = { Text("e.g. Move Medical notes to the top") },
                    modifier = Modifier.fillMaxWidth()
                )
                if (busy) {
                    Spacer(Modifier.height(8.dp))
                    LinearProgressIndicator(color = JomiGreen, modifier = Modifier.fillMaxWidth())
                }
            }
        },
        confirmButton = {
            Button(
                onClick = { onSubmit(instruction.trim()) },
                enabled = instruction.isNotBlank() && !busy,
                colors = ButtonDefaults.buttonColors(containerColor = JomiGreen)
            ) { Text(if (busy) "Thinking…" else "Propose") }
        },
        dismissButton = { TextButton(onClick = onDismiss, enabled = !busy) { Text("Cancel") } }
    )
}
