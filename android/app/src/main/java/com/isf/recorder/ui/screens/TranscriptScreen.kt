package com.isf.recorder.ui.screens

import android.widget.Toast
import androidx.compose.foundation.BorderStroke
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
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.isf.recorder.data.api.ISFApiClient
import com.isf.recorder.data.prefs.UserPreferences
import com.isf.recorder.ui.components.*
import com.isf.recorder.ui.theme.*
import kotlinx.coroutines.launch

/**
 * Transcript (round 3, new design): the player on top, the transcript as speaker turns with time
 * (tap a line to play from there), a search box. Admins also get "Transcript versions" (switch the
 * active version) and "Try another model". Replaces the old v1.2.0 detail screen.
 */
@Composable
fun TranscriptScreen(recordingId: String, onBack: () -> Unit) {
    val context = LocalContext.current
    val api = remember { ISFApiClient(context) }
    val isAdmin = remember { UserPreferences(context).isAdmin }
    val scope = rememberCoroutineScope()

    var detail by remember { mutableStateOf<Map<String, Any?>?>(null) }
    var error by remember { mutableStateOf<String?>(null) }
    var query by remember { mutableStateOf("") }
    var runs by remember { mutableStateOf<List<Map<String, Any?>>>(emptyList()) }
    var viewingRun by remember { mutableStateOf<Map<String, Any?>?>(null) }   // a non-active version
    var busy by remember { mutableStateOf(false) }
    var showModels by remember { mutableStateOf(false) }

    fun load() {
        scope.launch {
            api.getRecordingDetail(recordingId).onSuccess { detail = it; error = null }.onFailure { error = it.message }
            if (isAdmin) {
                @Suppress("UNCHECKED_CAST")
                api.getTranscriptRuns(recordingId).onSuccess { runs = (it["runs"] as? List<Map<String, Any?>>) ?: emptyList() }
            }
        }
    }
    LaunchedEffect(recordingId) { load() }

    val d = detail
    val title = d?.let { cleanTitle(it) } ?: "Transcript"
    val hasAudio = d?.get("has_audio") as? Boolean ?: false
    val controller = if (hasAudio) rememberAudioPlayerController(api.audioUrl(recordingId), audioWasCleanedUp(d)) else null

    @Suppress("UNCHECKED_CAST")
    val segments: List<Map<String, Any?>> = ((viewingRun ?: d)?.get("segments") as? List<Map<String, Any?>>) ?: emptyList()
    val gaps = silenceGapsOf(viewingRun ?: d)
    val shown = remember(segments, query) {
        val q = query.trim()
        if (q.isEmpty()) segments
        else segments.filter { (it["text"]?.toString() ?: "").contains(q, ignoreCase = true) ||
            speakerName(it["speaker"]).contains(q, ignoreCase = true) }
    }

    Column(Modifier.fillMaxSize().background(MaterialTheme.colorScheme.background)) {
        LazyColumn(
            modifier = Modifier.fillMaxSize(),
            contentPadding = PaddingValues(horizontal = 16.dp, vertical = 12.dp),
            verticalArrangement = Arrangement.spacedBy(8.dp)
        ) {
            item {
                BackHeader(title = "Transcript", subtitle = title, onBack = onBack)
                Spacer(Modifier.height(10.dp))
            }
            item {
                JomiCard(padding = 14.dp) {
                    when {
                        d == null && error == null -> Box(Modifier.fillMaxWidth(), contentAlignment = Alignment.Center) {
                            CircularProgressIndicator(color = JomiGreen, modifier = Modifier.size(24.dp), strokeWidth = 2.dp)
                        }
                        controller != null -> {
                            AudioPlayer(controller = controller, downloadName = title, fallbackDurationSec = recordingSeconds(d!!))
                            Spacer(Modifier.height(4.dp))
                            Text("Tap a line to play from there.", color = JomiTextMuted, fontSize = 11.sp)
                        }
                        d != null -> Text(audioUnavailableMessage(audioWasCleanedUp(d)) + " The transcript is kept.",
                            color = JomiTextMuted, fontSize = 13.sp)
                        else -> Text("Could not load: $error", color = RecordRed, fontSize = 13.sp)
                    }
                }
            }
            if (isAdmin && d != null) {
                item {
                    VersionsCard(
                        runs = runs,
                        viewingRunId = viewingRun?.get("id")?.toString(),
                        processing = d["status"]?.toString() == "processing",
                        busy = busy,
                        onView = { run ->
                            val id = run["id"]?.toString() ?: return@VersionsCard
                            if (run["is_active"] == true) { viewingRun = null; return@VersionsCard }
                            busy = true
                            scope.launch {
                                api.getTranscriptRun(recordingId, id)
                                    .onSuccess { viewingRun = it }
                                    .onFailure { Toast.makeText(context, it.message ?: "Could not load this version", Toast.LENGTH_SHORT).show() }
                                busy = false
                            }
                        },
                        onActivate = { id ->
                            busy = true
                            scope.launch {
                                api.activateTranscriptRun(recordingId, id)
                                    .onSuccess { Toast.makeText(context, "This version is now the transcript", Toast.LENGTH_SHORT).show(); viewingRun = null; load() }
                                    .onFailure { Toast.makeText(context, it.message ?: "Could not switch", Toast.LENGTH_LONG).show() }
                                busy = false
                            }
                        },
                        onTryModel = { showModels = true }
                    )
                }
            }
            item {
                OutlinedTextField(
                    value = query,
                    onValueChange = { query = it },
                    singleLine = true,
                    placeholder = { Text("Search the transcript") },
                    colors = OutlinedTextFieldDefaults.colors(focusedBorderColor = JomiGreen, unfocusedBorderColor = JomiBorder),
                    shape = RoundedCornerShape(14.dp),
                    modifier = Modifier.fillMaxWidth()
                )
                if (d != null && segments.isNotEmpty()) {
                    Spacer(Modifier.height(8.dp))
                    Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                        val textOut = { buildTranscriptText(d, segments, gaps) }
                        ActionChipButton("Copy", com.isf.recorder.R.drawable.ic_copy,
                            { copyOrShareText(context, "$title — transcript", textOut(), share = false, toast = "Transcript copied") },
                            modifier = Modifier.weight(1f))
                        ActionChipButton("Share", com.isf.recorder.R.drawable.ic_share,
                            { copyOrShareText(context, "$title — transcript", textOut(), share = true) },
                            modifier = Modifier.weight(1f))
                    }
                }
                if (viewingRun != null) {
                    Spacer(Modifier.height(6.dp))
                    StatusPill("Viewing another version: " + (viewingRun?.get("model_name") ?: viewingRun?.get("model") ?: ""), WarningOrange)
                }
            }
            if (d != null && segments.isEmpty()) {
                item { Text("No transcript yet.", color = JomiTextMuted, fontSize = 13.sp, modifier = Modifier.padding(8.dp)) }
            } else if (segments.isNotEmpty() && shown.isEmpty()) {
                item { Text("No line contains \"${query.trim()}\".", color = JomiTextMuted, fontSize = 13.sp, modifier = Modifier.padding(8.dp)) }
            }
            // lines, with a muted divider at each skipped-silence marker (not while searching)
            val rows: List<Any> = if (query.isNotBlank() || gaps.isEmpty()) shown else buildList {
                var g = 0
                for (seg in shown) {
                    val st = (seg["start_time"] as? Number)?.toDouble() ?: 0.0
                    while (g < gaps.size && gaps[g].first <= st) add(gaps[g++])
                    add(seg)
                }
                while (g < gaps.size) add(gaps[g++])
            }
            items(rows) { row ->
                if (row is Pair<*, *>) {
                    SilenceDivider((row.second as Double))
                } else {
                    @Suppress("UNCHECKED_CAST")
                    val seg = row as Map<String, Any?>
                    val start = (seg["start_time"] as? Number)?.toDouble() ?: 0.0
                    TranscriptLine(
                        speaker = speakerName(seg["speaker"]),
                        time = clockHms(start.toInt()),
                        text = seg["text"]?.toString().orEmpty(),
                        onClick = controller?.let { c -> { c.playFrom((start * 1000).toLong()) } }
                    )
                }
            }
            item { Spacer(Modifier.height(24.dp)) }
        }
    }

    if (showModels) {
        TryModelDialog(api = api, recordingId = recordingId, onDismiss = { showModels = false }, onStarted = {
            showModels = false
            load()
        })
    }
}

@Composable
private fun SilenceDivider(seconds: Double) {
    Row(Modifier.fillMaxWidth().padding(vertical = 2.dp), verticalAlignment = Alignment.CenterVertically) {
        HorizontalDivider(Modifier.weight(1f), color = JomiBorder)
        Text("  — silence ${gapDurationText(seconds)} skipped —  ", color = JomiTextMuted, fontSize = 11.sp)
        HorizontalDivider(Modifier.weight(1f), color = JomiBorder)
    }
}

/** "SPEAKER_01" -> "Speaker 1"; real names are kept. */
private fun speakerName(raw: Any?): String {
    val s = raw?.toString()?.trim().orEmpty()
    val m = Regex("^SPEAKER_?0*(\\d+)$", RegexOption.IGNORE_CASE).find(s)
    return when {
        s.isEmpty() -> "Speaker"
        m != null -> "Speaker ${m.groupValues[1].toInt().let { if (it == 0) 1 else it }}"
        else -> s
    }
}

@Composable
private fun TranscriptLine(speaker: String, time: String, text: String, onClick: (() -> Unit)?) {
    Surface(
        color = JomiCard,
        shape = RoundedCornerShape(14.dp),
        border = BorderStroke(1.dp, JomiBorder),
        modifier = Modifier.fillMaxWidth().then(if (onClick != null) Modifier.clickable(onClick = onClick) else Modifier)
    ) {
        Column(Modifier.padding(horizontal = 12.dp, vertical = 10.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(speaker, color = JomiTeal, fontWeight = FontWeight.Bold, fontSize = 13.sp, modifier = Modifier.weight(1f))
                Text(time, color = JomiTextMuted, fontSize = 12.sp)
            }
            Spacer(Modifier.height(3.dp))
            Text(text, fontSize = 14.sp, lineHeight = 20.sp, color = MaterialTheme.colorScheme.onBackground)
        }
    }
}

@Composable
private fun VersionsCard(
    runs: List<Map<String, Any?>>,
    viewingRunId: String?,
    processing: Boolean,
    busy: Boolean,
    onView: (Map<String, Any?>) -> Unit,
    onActivate: (String) -> Unit,
    onTryModel: () -> Unit
) {
    JomiCard(padding = 14.dp) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Text("Transcript versions", fontWeight = FontWeight.Bold, fontSize = 15.sp, modifier = Modifier.weight(1f))
            OutlinedButton(onClick = onTryModel, enabled = !processing && !busy, border = BorderStroke(1.dp, JomiMintBorder),
                contentPadding = PaddingValues(horizontal = 10.dp, vertical = 4.dp)) {
                Text("Try another model", color = JomiTeal, fontSize = 12.sp)
            }
        }
        if (processing) Text("A transcription is running…", color = JomiTextMuted, fontSize = 12.sp)
        if (runs.isEmpty()) {
            Text("Only the current transcript so far.", color = JomiTextMuted, fontSize = 12.sp)
        }
        runs.forEach { run ->
            val id = run["id"]?.toString() ?: return@forEach
            val active = run["is_active"] == true
            val status = run["status"]?.toString() ?: "completed"
            val viewing = viewingRunId == id || (viewingRunId == null && active)
            Spacer(Modifier.height(6.dp))
            Surface(
                color = if (viewing) JomiMint else JomiCard,
                shape = RoundedCornerShape(12.dp),
                border = BorderStroke(1.dp, if (viewing) JomiMintBorder else JomiBorder),
                modifier = Modifier.fillMaxWidth().clickable(enabled = status == "completed" && !busy) { onView(run) }
            ) {
                Row(Modifier.padding(10.dp), verticalAlignment = Alignment.CenterVertically) {
                    Column(Modifier.weight(1f)) {
                        Text(run["model_name"]?.toString() ?: run["model"]?.toString() ?: "Model",
                            fontWeight = FontWeight.SemiBold, fontSize = 13.sp, maxLines = 1, overflow = TextOverflow.Ellipsis)
                        val lines = (run["lines"] as? Number)?.toInt() ?: 0
                        val cost = (run["cost_inr"] as? Number)?.toDouble() ?: 0.0
                        Text(listOfNotNull(status.takeIf { it != "completed" }, "$lines lines", "₹%.2f".format(cost)).joinToString(" · "),
                            color = JomiTextMuted, fontSize = 11.sp)
                    }
                    when {
                        active -> StatusPill("In use", JomiTeal)
                        viewingRunId == id && status == "completed" -> TextButton(onClick = { onActivate(id) }, enabled = !busy) {
                            Text("Use this", color = JomiTeal, fontWeight = FontWeight.Bold)
                        }
                    }
                }
            }
        }
    }
}

@Composable
private fun TryModelDialog(api: ISFApiClient, recordingId: String, onDismiss: () -> Unit, onStarted: () -> Unit) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    var models by remember { mutableStateOf<List<Map<String, Any?>>>(emptyList()) }
    var picked by remember { mutableStateOf<String?>(null) }
    var starting by remember { mutableStateOf(false) }
    LaunchedEffect(Unit) {
        api.getAvailableModels().onSuccess { list ->
            models = list.filter { m -> (m["supports"] as? List<*>)?.contains("transcription") != false }
        }
    }
    AlertDialog(
        onDismissRequest = { if (!starting) onDismiss() },
        title = { Text("Try another model") },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(6.dp)) {
                Text("The audio is transcribed again; you can compare and pick the version to use.", color = JomiTextMuted, fontSize = 12.sp)
                if (models.isEmpty()) CircularProgressIndicator(color = JomiGreen, modifier = Modifier.size(22.dp), strokeWidth = 2.dp)
                models.forEach { m ->
                    val id = m["id"]?.toString() ?: return@forEach
                    Surface(
                        color = if (picked == id) JomiMint else JomiCard,
                        shape = RoundedCornerShape(10.dp),
                        border = BorderStroke(1.dp, if (picked == id) JomiMintBorder else JomiBorder),
                        modifier = Modifier.fillMaxWidth().clickable(enabled = !starting) { picked = id }
                    ) {
                        Column(Modifier.padding(10.dp)) {
                            Text(m["name"]?.toString() ?: id, fontWeight = FontWeight.SemiBold, fontSize = 13.sp)
                            m["description"]?.toString()?.takeIf { it.isNotBlank() }?.let { Text(it, color = JomiTextMuted, fontSize = 11.sp) }
                        }
                    }
                }
            }
        },
        confirmButton = {
            Button(
                onClick = {
                    val model = picked ?: return@Button
                    starting = true
                    scope.launch {
                        api.reTranscribeWithModel(recordingId, model)
                            .onSuccess { Toast.makeText(context, "Started — the new version appears when it is done", Toast.LENGTH_LONG).show(); onStarted() }
                            .onFailure { Toast.makeText(context, it.message ?: "Could not start", Toast.LENGTH_LONG).show() }
                        starting = false
                    }
                },
                enabled = picked != null && !starting,
                colors = ButtonDefaults.buttonColors(containerColor = JomiGreen)
            ) { Text(if (starting) "Starting…" else "Start") }
        },
        dismissButton = { TextButton(onClick = onDismiss, enabled = !starting) { Text("Cancel") } }
    )
}
