package com.isf.recorder.ui.components

import android.content.Context
import android.widget.Toast
import androidx.compose.foundation.ExperimentalFoundationApi
import androidx.compose.foundation.combinedClickable
import androidx.compose.foundation.layout.*
import androidx.compose.material3.*
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.isf.recorder.R
import com.isf.recorder.data.api.ISFApiClient
import com.isf.recorder.ui.theme.*

/**
 * One recording in Home "Previous recordings" and See all.
 * Line 1: "6 Oct 2026 · Meeting" + status pill. Title. ONE line "Recording time · 34 min … Cost ₹x"
 * (maxLines = 1 — fixes the old card whose duration wrapped vertically). Then Report / AI Chat /
 * edit / delete and Copy / Share.
 */
@OptIn(ExperimentalFoundationApi::class)
@Composable
fun RecordingCard(
    rec: Map<String, Any?>,
    onOpen: () -> Unit,
    onReport: () -> Unit,
    onChat: () -> Unit,
    onCopy: () -> Unit,
    onShare: () -> Unit,
    modifier: Modifier = Modifier,
    showOwner: Boolean = false,
    onEditTitle: (() -> Unit)? = null,
    onDelete: (() -> Unit)? = null,
    selectionMode: Boolean = false,
    selected: Boolean = false,
    onToggleSelect: (() -> Unit)? = null,
    onLongPress: (() -> Unit)? = null,   // long-press starts merge/delete selection
    highlighted: Boolean = false         // currently loaded in the Home player
) {
    val status = rec["status"]?.toString()
    val reportStatus = rec["report_status"]?.toString()
    val (statusText, statusColor) = statusLabel(status, reportStatus)
    val modeText = modeLabel(rec["mode"])
    val dateText = recordingDate(rec)
    val cost = rec["estimated_cost_formatted"]?.toString()?.takeIf { it.isNotBlank() } ?: "₹—"
    val owner = rec["user_name"]?.toString()?.takeIf { it.isNotBlank() } ?: rec["recorded_by_name"]?.toString()

    JomiCard(
        modifier = modifier.combinedClickable(
            onClick = { if (selectionMode) onToggleSelect?.invoke() else onOpen() },
            onLongClick = { if (selectionMode) onToggleSelect?.invoke() else onLongPress?.invoke() }
        ),
        border = if (selected || highlighted) JomiGreen else JomiBorder,
        color = if (selected) JomiMint.copy(alpha = 0.5f) else JomiCard
    ) {
        Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.fillMaxWidth()) {
            if (selectionMode) {
                Checkbox(
                    checked = selected,
                    onCheckedChange = { onToggleSelect?.invoke() },
                    colors = CheckboxDefaults.colors(checkedColor = JomiGreen),
                    modifier = Modifier.size(28.dp)
                )
                Spacer(Modifier.width(6.dp))
            }
            Text(
                text = listOfNotNull(dateText.takeIf { it.isNotBlank() }, modeText).joinToString(" · "),
                color = JomiTextMuted,
                fontSize = 12.sp,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis,
                modifier = Modifier.weight(1f)
            )
            StatusPill(statusText, statusColor)
        }
        Spacer(Modifier.height(6.dp))
        Text(
            text = cleanTitle(rec),
            color = MaterialTheme.colorScheme.onBackground,
            fontSize = 16.sp,
            fontWeight = FontWeight.SemiBold,
            maxLines = 2,
            overflow = TextOverflow.Ellipsis
        )
        recordingPeopleLine(rec)?.let { people ->
            Text(people, color = JomiTextMuted, fontSize = 13.sp, maxLines = 1, overflow = TextOverflow.Ellipsis)
        }
        silenceSkippedLine(rec)?.let { Text(it, color = JomiTextMuted, fontSize = 12.sp, maxLines = 1) }
        if (showOwner && !owner.isNullOrBlank()) {
            Text("🎙️ Recorded by $owner", color = JomiTeal, fontSize = 13.sp, fontWeight = FontWeight.SemiBold, maxLines = 1, overflow = TextOverflow.Ellipsis)
        }
        Spacer(Modifier.height(8.dp))
        // ONE line: never wraps vertically.
        Row(modifier = Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
            Text(
                text = "Recording time · ${recordingDurationText(rec)}",
                color = JomiTextMuted,
                fontSize = 12.sp,
                maxLines = 1,
                softWrap = false,
                overflow = TextOverflow.Ellipsis,
                modifier = Modifier.weight(1f)
            )
            Spacer(Modifier.width(8.dp))
            Text(
                text = "Cost · $cost",
                color = JomiTextMuted,
                fontSize = 12.sp,
                maxLines = 1,
                softWrap = false
            )
        }

        if (!selectionMode) {
            Spacer(Modifier.height(12.dp))
            Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                ActionChipButton("Report", R.drawable.ic_report, onReport)
                ActionChipButton(
                    "AI Chat", R.drawable.ic_chat, onChat,
                    tint = MaterialTheme.colorScheme.onBackground, container = JomiCard, border = JomiBorder
                )
                Spacer(Modifier.weight(1f))
                if (onEditTitle != null) {
                    SmallIconButton(R.drawable.ic_edit, "Edit title", onEditTitle, tint = JomiGreen, background = JomiMint)
                }
                if (onDelete != null) {
                    SmallIconButton(R.drawable.ic_delete, "Erase recording", onDelete)
                }
            }
            Spacer(Modifier.height(12.dp))
            HorizontalDivider(color = JomiBorder)
            Spacer(Modifier.height(12.dp))
            Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                ActionChipButton("Copy", R.drawable.ic_copy, onCopy, modifier = Modifier.weight(1f))
                ActionChipButton("Share", R.drawable.ic_share, onShare, modifier = Modifier.weight(1f))
            }
        }
    }
}

/**
 * Text for Copy / Share of a recording: the report if there is one, otherwise the AI overview.
 */
suspend fun buildRecordingShareText(api: ISFApiClient, rec: Map<String, Any?>): String {
    val id = rec["id"]?.toString().orEmpty()
    val title = cleanTitle(rec)
    val header = buildString {
        append(title)
        val meta = listOfNotNull(recordingDate(rec).takeIf { it.isNotBlank() }, modeLabel(rec["mode"]), recordingDurationText(rec))
        append("\n").append(meta.joinToString(" · "))
        recordingPeopleLine(rec)?.let { append("\n").append(it) }
    }
    val report = if (id.isNotBlank()) api.getReport(id).getOrNull() else null
    val reportText = report?.get("content")?.toString()?.takeIf { it.isNotBlank() }
    if (reportText != null) return "$header\n\n$reportText"
    // One report only (round 2): no fallback to the old AI summary.
    return "$header\n\n(The report is not ready yet.)"
}

/**
 * Share of a class / meeting report (21 Oct): WhatsApp cuts long messages, so Share sends the title, the details
 * and a LINK to a web page with the full report (in Hindi when [lang] is "hi"). Copy still copies the full text.
 * No link (no internet, report not ready…) → the full text is shared as before.
 */
suspend fun shareReportLink(
    context: Context, api: ISFApiClient, recordingId: String, title: String, details: List<String>,
    lang: String?, fullText: suspend () -> String
) {
    val hindi = lang == "hi"
    api.getReportShareLink(recordingId, lang?.takeIf { hindi })
        .onSuccess { url ->
            val message = buildString {
                append("*").append(title).append("*")
                details.forEach { append("\n").append(it) }
                append("\n\n📄 ").append(if (hindi) "पूरी रिपोर्ट (खोलने के लिए टैप करें):" else "Full report (tap to open):")
                append("\n").append(url)
            }
            shareText(context, title, message)
        }
        .onFailure {
            Toast.makeText(context, "Could not make a link (${it.message ?: "no internet"}) — sharing the full text", Toast.LENGTH_LONG).show()
            shareText(context, title, markdownToChatText(fullText()))
        }
}

/** Copy or share a recording's report/summary (fetches it first). */
suspend fun copyOrShareRecording(context: Context, api: ISFApiClient, rec: Map<String, Any?>, share: Boolean) {
    try {
        val id = rec["id"]?.toString().orEmpty()
        if (share && id.isNotBlank() && rec["source"]?.toString() != "call") {
            shareReportLink(context, api, id, cleanTitle(rec), reportDetailsLines(rec), null) { buildRecordingShareText(api, rec) }
            return
        }
        val text = buildRecordingShareText(api, rec)
        copyOrShareText(context, cleanTitle(rec), text, share)
    } catch (e: Exception) {
        Toast.makeText(context, e.message ?: "Could not prepare the text", Toast.LENGTH_SHORT).show()
    }
}
