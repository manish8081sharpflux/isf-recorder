package com.isf.recorder.ui.components

import android.widget.Toast
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.isf.recorder.ISFApplication
import com.isf.recorder.R
import com.isf.recorder.data.api.ISFApiClient
import com.isf.recorder.ui.theme.*
import kotlinx.coroutines.launch

/** Keeps the long-press selection (recording ids) when the person opens a report and comes Back. */
val SelectedIdsSaver = androidx.compose.runtime.saveable.listSaver<Set<String>, String>(
    save = { it.toList() },
    restore = { it.toSet() }
)

/** Bottom bar shown after a long-press: "N selected" + Merge + Delete. */
@Composable
fun SelectionActionBar(
    count: Int,
    onMerge: () -> Unit,
    onDelete: () -> Unit,
    onCancel: () -> Unit,
    modifier: Modifier = Modifier
) {
    Surface(
        color = Color.White,
        shape = RoundedCornerShape(16.dp),
        border = BorderStroke(1.dp, JomiBorder),
        shadowElevation = 6.dp,
        modifier = modifier.fillMaxWidth()
    ) {
        Row(Modifier.padding(horizontal = 12.dp, vertical = 10.dp), verticalAlignment = Alignment.CenterVertically) {
            SmallIconButton(R.drawable.ic_close, "Cancel selection", onCancel, tint = MaterialTheme.colorScheme.onBackground)
            Text("$count selected", fontWeight = FontWeight.SemiBold, fontSize = 15.sp, modifier = Modifier.weight(1f).padding(start = 4.dp))
            OutlinedButton(
                onClick = onDelete,
                enabled = count >= 1,
                border = BorderStroke(1.dp, RecordRed.copy(alpha = 0.4f)),
                colors = ButtonDefaults.outlinedButtonColors(contentColor = RecordRed),
                contentPadding = PaddingValues(horizontal = 12.dp, vertical = 6.dp)
            ) { Text("Delete") }
            Spacer(Modifier.width(8.dp))
            Button(
                onClick = onMerge,
                enabled = count >= 2,
                colors = ButtonDefaults.buttonColors(containerColor = JomiGreen),
                contentPadding = PaddingValues(horizontal = 14.dp, vertical = 6.dp)
            ) { Text("Merge", fontWeight = FontWeight.Bold) }
        }
    }
}

/** Merge the selected recordings (stitched in order, transcribed again as one). */
@Composable
fun MergeDialog(
    api: ISFApiClient,
    recordings: List<Map<String, Any?>>,
    onDismiss: () -> Unit,
    onStarted: () -> Unit,
    onMerged: (newRecordingId: String?) -> Unit
) {
    val context = LocalContext.current
    var title by remember {
        mutableStateOf(
            ("Merged: " + recordings.map { cleanTitle(it) }.filterNot { it.startsWith("Untitled") }.joinToString(" + "))
                .take(60).ifBlank { "Merged recording" }
        )
    }
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("Merge ${recordings.size} recordings", fontWeight = FontWeight.Bold) },
        text = {
            Column {
                Text("The audio is joined in order and transcribed again as one recording with one report.", fontSize = 13.sp)
                Spacer(Modifier.height(10.dp))
                OutlinedTextField(value = title, onValueChange = { title = it }, label = { Text("Title") }, singleLine = true, modifier = Modifier.fillMaxWidth())
            }
        },
        confirmButton = {
            Button(colors = ButtonDefaults.buttonColors(containerColor = JomiGreen), onClick = {
                val ids = recordings.mapNotNull { it["id"]?.toString() }
                onStarted()
                // Application scope: leaving the screen must not cancel the merge.
                ISFApplication.applicationScope.launch {
                    val res = api.mergeRecordings(ids, title.trim())
                    res.onSuccess { Toast.makeText(context, "Merged — transcribing now", Toast.LENGTH_LONG).show(); onMerged(it["recording_id"]?.toString()) }
                        .onFailure { Toast.makeText(context, "Merge failed: ${it.message}", Toast.LENGTH_LONG).show(); onMerged(null) }
                }
            }) { Text("Merge") }
        },
        dismissButton = { TextButton(onClick = onDismiss) { Text("Cancel") } }
    )
}

/** Erase several recordings for real, behind the strong confirmation. */
@Composable
fun BulkEraseDialog(
    api: ISFApiClient,
    recordings: List<Map<String, Any?>>,
    onDismiss: () -> Unit,
    onDone: () -> Unit
) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    var busy by remember { mutableStateOf(false) }
    StrongDeleteDialog(
        itemTitle = if (recordings.size == 1) cleanTitle(recordings[0]) else "${recordings.size} recordings",
        title = if (recordings.size == 1) "Erase this recording forever?" else "Erase ${recordings.size} recordings forever?",
        busy = busy,
        onDismiss = onDismiss,
        onConfirm = {
            busy = true
            scope.launch {
                var failed = 0
                recordings.forEach { r -> if (api.eraseRecording(r["id"].toString()).isFailure) failed++ }
                busy = false
                Toast.makeText(
                    context,
                    if (failed == 0) "Erased ${recordings.size}" else "Erased ${recordings.size - failed}, $failed could not be erased",
                    Toast.LENGTH_LONG
                ).show()
                onDone()
            }
        }
    )
}
