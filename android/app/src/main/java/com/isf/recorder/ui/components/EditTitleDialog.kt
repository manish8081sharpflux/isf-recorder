package com.isf.recorder.ui.components

import androidx.compose.foundation.layout.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.isf.recorder.ui.theme.RecordRed
import kotlinx.coroutines.launch

/** Rename dialog used for recordings and conversations. [onSave] returns an error message or null. */
@Composable
fun EditTitleDialog(
    initial: String,
    onDismiss: () -> Unit,
    onSave: suspend (String) -> String?,
    title: String = "Edit title"
) {
    var value by remember { mutableStateOf(initial) }
    var busy by remember { mutableStateOf(false) }
    var error by remember { mutableStateOf<String?>(null) }
    val scope = rememberCoroutineScope()
    AlertDialog(
        onDismissRequest = { if (!busy) onDismiss() },
        title = { Text(title, fontWeight = FontWeight.Bold) },
        text = {
            Column {
                OutlinedTextField(
                    value = value,
                    onValueChange = { value = it; error = null },
                    singleLine = true,
                    modifier = Modifier.fillMaxWidth()
                )
                if (error != null) {
                    Spacer(Modifier.height(6.dp))
                    Text(error!!, color = RecordRed, fontSize = 12.sp)
                }
            }
        },
        confirmButton = {
            TextButton(
                enabled = !busy && value.isNotBlank(),
                onClick = {
                    busy = true
                    scope.launch {
                        val err = onSave(value.trim())
                        busy = false
                        if (err == null) onDismiss() else error = err
                    }
                }
            ) { Text(if (busy) "Saving…" else "Save") }
        },
        dismissButton = { TextButton(onClick = onDismiss, enabled = !busy) { Text("Cancel") } }
    )
}

