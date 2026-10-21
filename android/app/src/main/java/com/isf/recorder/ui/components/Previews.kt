package com.isf.recorder.ui.components

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.*
import androidx.compose.material3.MaterialTheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.tooling.preview.Preview
import androidx.compose.ui.unit.dp
import com.isf.recorder.ui.theme.ISFRecorderTheme

private val sampleRecording = mapOf<String, Any?>(
    "id" to "preview",
    "title" to "Mohor Boys · Daily schedule with a long title that must ellipsize nicely",
    "mode" to "schedule",
    "status" to "completed",
    "report_status" to "draft",
    "date_formatted" to "6 Oct 2026",
    "voice_seconds" to 2040,
    "estimated_cost_formatted" to "₹12.40",
    "user_name" to "Asmita"
)

@Preview(showBackground = true, widthDp = 340)
@Composable
private fun RecordingCardPreview() {
    ISFRecorderTheme {
        Column(Modifier.background(MaterialTheme.colorScheme.background).padding(16.dp)) {
            RecordingCard(
                rec = sampleRecording, onOpen = {}, onReport = {}, onChat = {}, onCopy = {}, onShare = {},
                showOwner = true, onEditTitle = {}, onDelete = {}
            )
        }
    }
}

@Preview(showBackground = true, widthDp = 340)
@Composable
private fun TranscriptionStatusCardPreview() {
    ISFRecorderTheme {
        Column(Modifier.background(MaterialTheme.colorScheme.background).padding(16.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
            TranscriptionStatusCard(TranscriptionUi.Transcribing, {}, {})
            TranscriptionStatusCard(TranscriptionUi.Failed("Gemini did not respond."), {}, {})
            TranscriptionStatusCard(TranscriptionUi.Done(hasReport = true), {}, {})
        }
    }
}
