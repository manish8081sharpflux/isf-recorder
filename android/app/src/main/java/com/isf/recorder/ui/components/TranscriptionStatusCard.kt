package com.isf.recorder.ui.components

import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.*
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.isf.recorder.R
import com.isf.recorder.ui.theme.*

/** What the status card shows for one finished recording (derived from GET /recordings/status). */
sealed class TranscriptionUi {
    object Saving : TranscriptionUi()            // Stop pressed, chunks uploading / /finish pending
    object Offline : TranscriptionUi()           // no recording id yet (saved on phone)
    object Transcribing : TranscriptionUi()
    object WritingReport : TranscriptionUi()
    data class Done(val hasReport: Boolean, val noSpeech: Boolean = false) : TranscriptionUi()
    data class Failed(val message: String, val reportOnly: Boolean = false) : TranscriptionUi()
}

/** Maps a status item ({status, pipeline_stage, report_status, error_message, title}) to UI. */
fun transcriptionUiFrom(item: Map<String, Any?>?): TranscriptionUi {
    if (item == null) return TranscriptionUi.Saving
    val status = item["status"]?.toString()
    val report = item["report_status"]?.toString()
    val stage = item["pipeline_stage"]?.toString()
    val error = item["error_message"]?.toString()?.takeIf { it.isNotBlank() && it != "null" }
    return when (status) {
        "recording", "uploading" -> TranscriptionUi.Saving
        "processing", "stitching", "pending" -> when (stage) {
            "awaiting_chunks", "stitching" -> TranscriptionUi.Saving
            "reporting" -> TranscriptionUi.WritingReport
            else -> TranscriptionUi.Transcribing
        }
        "error" -> TranscriptionUi.Failed(error ?: "Transcription failed.")
        "completed" -> when (report) {
            "generating" -> TranscriptionUi.WritingReport
            "draft", "final" -> TranscriptionUi.Done(hasReport = true)
            "error" -> TranscriptionUi.Failed(error ?: "The transcript is ready but the report could not be written.", reportOnly = true)
            // transcript committed, report row not created yet: the pipeline is still writing it
            else -> if (stage == "reporting") TranscriptionUi.WritingReport else TranscriptionUi.Done(
                hasReport = false,
                noSpeech = item["title"]?.toString()?.contains("No speech detected", ignoreCase = true) == true
            )
        }
        else -> TranscriptionUi.Saving
    }
}

/** True while the card should keep polling. */
fun TranscriptionUi.isActive(): Boolean = when (this) {
    TranscriptionUi.Saving, TranscriptionUi.Transcribing, TranscriptionUi.WritingReport -> true
    else -> false
}

/**
 * Mint card from the mockup: rotating loader + "Transcribing… Your report will be ready shortly.";
 * error → message + Retry; done → Report button.
 */
@Composable
fun TranscriptionStatusCard(
    state: TranscriptionUi,
    onRetry: () -> Unit,
    onOpenReport: () -> Unit,
    modifier: Modifier = Modifier,
    retrying: Boolean = false
) {
    val isError = state is TranscriptionUi.Failed
    val bg = if (isError) RecordRed.copy(alpha = 0.06f) else JomiMint
    val border = if (isError) RecordRed.copy(alpha = 0.3f) else JomiMintBorder
    Surface(
        color = bg,
        shape = RoundedCornerShape(18.dp),
        border = BorderStroke(1.dp, border),
        modifier = modifier.fillMaxWidth()
    ) {
        Row(modifier = Modifier.padding(16.dp), verticalAlignment = Alignment.CenterVertically) {
            when {
                state.isActive() || retrying -> CircularProgressIndicator(
                    color = JomiGreen,
                    trackColor = JomiMintBorder.copy(alpha = 0.5f),
                    strokeWidth = 3.dp,
                    modifier = Modifier.size(28.dp)
                )
                isError -> Icon(painterResource(R.drawable.ic_warning), null, tint = RecordRed, modifier = Modifier.size(26.dp))
                else -> Icon(painterResource(R.drawable.ic_check), null, tint = JomiGreen, modifier = Modifier.size(26.dp))
            }
            Spacer(Modifier.width(14.dp))
            val (title, body) = when (state) {
                TranscriptionUi.Saving -> "Uploading…" to "Sending the last pieces of audio."
                TranscriptionUi.Offline -> "Saved on phone — will upload when online" to "Transcription starts after the upload."
                TranscriptionUi.Transcribing -> "Transcribing…" to "Your report will be ready shortly."
                TranscriptionUi.WritingReport -> "Writing your report…" to "Almost done."
                is TranscriptionUi.Done -> when {
                    state.noSpeech -> "No speech detected" to "Nothing to transcribe in this recording."
                    state.hasReport -> "Report ready" to "Open it to read, edit or share."
                    else -> "Transcript ready" to "Open the report to view or generate it."
                }
                is TranscriptionUi.Failed -> (if (state.reportOnly) "Report failed" else "Transcription failed") to state.message
            }
            Column(modifier = Modifier.weight(1f)) {
                Text(title, color = if (isError) RecordRed else JomiTeal, fontSize = 16.sp, fontWeight = FontWeight.Bold)
                Spacer(Modifier.height(2.dp))
                Text(body, color = Color(0xFF374151), fontSize = 13.sp)
            }
            when {
                isError -> Button(
                    onClick = onRetry,
                    enabled = !retrying,
                    shape = RoundedCornerShape(10.dp),
                    colors = ButtonDefaults.buttonColors(containerColor = RecordRed, contentColor = Color.White),
                    contentPadding = PaddingValues(horizontal = 14.dp, vertical = 6.dp)
                ) { Text("Retry", fontWeight = FontWeight.Bold) }
                state is TranscriptionUi.Done && !state.noSpeech -> Button(
                    onClick = onOpenReport,
                    shape = RoundedCornerShape(10.dp),
                    colors = ButtonDefaults.buttonColors(containerColor = JomiGreen, contentColor = Color.White),
                    contentPadding = PaddingValues(horizontal = 14.dp, vertical = 6.dp)
                ) {
                    Icon(painterResource(R.drawable.ic_report), null, modifier = Modifier.size(16.dp))
                    Spacer(Modifier.width(6.dp))
                    Text("Report", fontWeight = FontWeight.Bold)
                }
            }
        }
    }
}
