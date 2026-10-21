package com.isf.recorder.ui.screens

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.input.KeyboardCapitalization
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.isf.recorder.data.api.ISFApiClient
import com.isf.recorder.ui.components.JomiCard
import com.isf.recorder.ui.theme.*
import kotlinx.coroutines.launch

/** First login: "What's your name?" (saved with PATCH /auth/me). Shown until a real name is set. */
@Composable
fun AskNameScreen(api: ISFApiClient, onDone: () -> Unit) {
    val scope = rememberCoroutineScope()
    var name by remember { mutableStateOf("") }
    var saving by remember { mutableStateOf(false) }
    var error by remember { mutableStateOf<String?>(null) }
    val clean = name.trim().split(' ', '\t', '\n').filter { it.isNotEmpty() }.joinToString(" ")
    val valid = clean.length in 2..60

    fun save() {
        if (!valid || saving) return
        saving = true
        error = null
        scope.launch {
            api.updateMyName(clean)
                .onSuccess { onDone() }
                .onFailure { error = it.message ?: "Could not save your name" }
            saving = false
        }
    }

    Box(Modifier.fillMaxSize().background(MaterialTheme.colorScheme.background).padding(20.dp), contentAlignment = Alignment.Center) {
        JomiCard {
            Text("Welcome to Jomi", color = JomiTeal, fontSize = 13.sp, fontWeight = FontWeight.Bold)
            Spacer(Modifier.height(4.dp))
            Text("What's your name?", fontSize = 22.sp, fontWeight = FontWeight.Bold)
            Spacer(Modifier.height(6.dp))
            Text("Your team sees this name on your recordings.", color = JomiTextMuted, fontSize = 13.sp)
            Spacer(Modifier.height(16.dp))
            OutlinedTextField(
                value = name,
                onValueChange = { if (it.length <= 60) name = it },
                singleLine = true,
                label = { Text("Your name") },
                isError = error != null,
                keyboardOptions = KeyboardOptions(capitalization = KeyboardCapitalization.Words, imeAction = ImeAction.Done),
                keyboardActions = KeyboardActions(onDone = { save() }),
                colors = OutlinedTextFieldDefaults.colors(focusedBorderColor = JomiGreen),
                modifier = Modifier.fillMaxWidth()
            )
            error?.let { Spacer(Modifier.height(6.dp)); Text(it, color = RecordRed, fontSize = 12.sp) }
            Spacer(Modifier.height(16.dp))
            Button(
                onClick = { save() },
                enabled = valid && !saving,
                colors = ButtonDefaults.buttonColors(containerColor = JomiGreen),
                modifier = Modifier.fillMaxWidth().height(48.dp)
            ) { Text(if (saving) "Saving…" else "Continue", fontWeight = FontWeight.Bold) }
        }
    }
}
