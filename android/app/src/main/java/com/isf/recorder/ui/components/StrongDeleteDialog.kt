package com.isf.recorder.ui.components

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.isf.recorder.R
import com.isf.recorder.ui.theme.*

/**
 * Erasing a recording is permanent (contract §4.8): the user must tick
 * "I understand this cannot be undone" before the red button works.
 */
@Composable
fun StrongDeleteDialog(
    itemTitle: String,
    onConfirm: () -> Unit,
    onDismiss: () -> Unit,
    busy: Boolean = false,
    title: String = "Erase this recording forever?",
    whatIsDeleted: List<String> = listOf("the audio", "the transcript", "the report", "all AI chats about it"),
    confirmLabel: String = "Erase forever"
) {
    var understood by remember { mutableStateOf(false) }
    AlertDialog(
        onDismissRequest = { if (!busy) onDismiss() },
        icon = { Icon(painterResource(R.drawable.ic_warning), null, tint = RecordRed) },
        title = { Text(title, fontWeight = FontWeight.Bold) },
        text = {
            Column {
                Text("\"$itemTitle\"", fontWeight = FontWeight.SemiBold, fontSize = 14.sp)
                Spacer(Modifier.height(8.dp))
                Text("This permanently deletes:", fontSize = 13.sp)
                whatIsDeleted.forEach { Text("•  $it", fontSize = 13.sp) }
                Spacer(Modifier.height(6.dp))
                Text("Nobody — not even an admin — can bring it back.", color = RecordRed, fontSize = 13.sp, fontWeight = FontWeight.Medium)
                Spacer(Modifier.height(10.dp))
                Row(
                    verticalAlignment = Alignment.CenterVertically,
                    modifier = Modifier.fillMaxWidth().clickable(enabled = !busy) { understood = !understood }
                ) {
                    Checkbox(
                        checked = understood,
                        onCheckedChange = { understood = it },
                        enabled = !busy,
                        colors = CheckboxDefaults.colors(checkedColor = RecordRed)
                    )
                    Text("I understand this cannot be undone", fontSize = 13.sp)
                }
            }
        },
        confirmButton = {
            Button(
                onClick = onConfirm,
                enabled = understood && !busy,
                colors = ButtonDefaults.buttonColors(containerColor = RecordRed, contentColor = Color.White)
            ) {
                if (busy) {
                    CircularProgressIndicator(modifier = Modifier.size(16.dp), color = Color.White, strokeWidth = 2.dp)
                } else {
                    Text(confirmLabel, fontWeight = FontWeight.Bold)
                }
            }
        },
        dismissButton = { TextButton(onClick = onDismiss, enabled = !busy) { Text("Keep it") } }
    )
}
