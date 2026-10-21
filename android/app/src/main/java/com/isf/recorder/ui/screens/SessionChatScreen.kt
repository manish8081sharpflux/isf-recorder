package com.isf.recorder.ui.screens

import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.widget.Toast
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.isf.recorder.data.api.ISFApiClient
import com.isf.recorder.ui.components.SlideBackContainer
import com.isf.recorder.ui.theme.*
import kotlinx.coroutines.launch

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun SessionChatScreen(
    mode: String, // "session" | "all_sessions"
    recordingId: String? = null,
    recordingTitle: String? = null,
    initialPrompt: String? = null,
    conversationId: String? = null,   // saved conversation (contract §6)
    newChat: Boolean = false,         // start a fresh conversation on the first message
    onBack: () -> Unit
) {
    val context = LocalContext.current
    val apiClient = remember { ISFApiClient(context) }
    val scope = rememberCoroutineScope()
    val listState = rememberLazyListState()

    var messages by remember { mutableStateOf<List<Map<String, Any?>>>(emptyList()) }
    var isLoadingHistory by remember { mutableStateOf(true) }
    var isSending by remember { mutableStateOf(false) }
    // When the AI answers, close the keyboard so the whole answer can be read; tapping the box opens it again.
    val keyboard = androidx.compose.ui.platform.LocalSoftwareKeyboardController.current
    val focusManager = androidx.compose.ui.platform.LocalFocusManager.current
    var inputText by remember { mutableStateOf(initialPrompt ?: "") }
    var errorMessage by remember { mutableStateOf<String?>(null) }

    // Templates
    var templates by remember { mutableStateOf<List<Map<String, Any?>>>(emptyList()) }
    var isLoadingTemplates by remember { mutableStateOf(false) }
    var showTemplateSheet by remember { mutableStateOf(false) }

    // Saved conversation this screen is bound to (null until resolved / created)
    var convId by remember { mutableStateOf(conversationId) }
    var convTitle by remember { mutableStateOf<String?>(null) }
    // Server without /chat/conversations → fall back to the v1.2.0 endpoints
    var useLegacy by remember { mutableStateOf(false) }

    // Title & Subtitle based on mode
    val headerTitle = convTitle?.takeIf { it.isNotBlank() } ?: if (mode == "session") {
        recordingTitle ?: "Session AI Chat"
    } else {
        "Ask Jomi"
    }

    val headerSubtitle = if (mode == "session") {
        "Session Memory • Powered by Gemini"
    } else {
        "All Sessions Memory • Powered by Gemini"
    }

    fun copyToClipboard(text: String) {
        val clipboard = context.getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
        val clip = ClipData.newPlainText("AI Response", text)
        clipboard.setPrimaryClip(clip)
        Toast.makeText(context, "Copied to clipboard", Toast.LENGTH_SHORT).show()
    }

    fun shareText(text: String) {
        try {
            val sendIntent = Intent(Intent.ACTION_SEND).apply {
                putExtra(Intent.EXTRA_TEXT, text)
                type = "text/plain"
            }
            val shareIntent = Intent.createChooser(sendIntent, "Share Message")
            context.startActivity(shareIntent)
        } catch (e: Exception) {
            Toast.makeText(context, "Failed to share: ${e.message}", Toast.LENGTH_SHORT).show()
        }
    }

    fun isMissingEndpoint(e: Throwable?) =
        e is ISFApiClient.ApiException && (e.code == 404 || e.code == 405)

    fun loadHistory() {
        isLoadingHistory = true
        errorMessage = null
        scope.launch {
            if (!useLegacy && convId == null && !newChat) {
                // Open the latest conversation for this scope (a recording, or general Ask Jomi).
                val list = apiClient.listConversations(mode, if (mode == "session") recordingId else null)
                if (list.isSuccess) {
                    convId = list.getOrNull()?.firstOrNull()?.get("id")?.toString()
                } else if (isMissingEndpoint(list.exceptionOrNull())) {
                    useLegacy = true
                }
            }
            val currentConv = convId
            val res: Result<List<Map<String, Any?>>> = when {
                useLegacy -> apiClient.getChatHistory(recordingId, mode)
                currentConv != null -> apiClient.getConversationMessages(currentConv).map { data ->
                    @Suppress("UNCHECKED_CAST")
                    val conv = data["conversation"] as? Map<String, Any?>
                    conv?.get("title")?.toString()?.takeIf { it.isNotBlank() && it != "null" }?.let { convTitle = it }
                    @Suppress("UNCHECKED_CAST")
                    (data["messages"] as? List<Map<String, Any?>>) ?: emptyList()
                }
                else -> Result.success(emptyList())
            }
            isLoadingHistory = false
            if (res.isSuccess) {
                messages = res.getOrNull() ?: emptyList()
                if (messages.isNotEmpty()) {
                    scope.launch {
                        listState.animateScrollToItem(messages.size - 1)
                    }
                }
            } else {
                errorMessage = res.exceptionOrNull()?.message ?: "Failed to load chat history"
            }
        }
    }

    fun loadTemplates() {
        isLoadingTemplates = true
        scope.launch {
            val res = apiClient.listChatTemplates()
            isLoadingTemplates = false
            if (res.isSuccess) {
                templates = res.getOrNull() ?: emptyList()
            }
        }
    }

    fun sendMessage(textToSend: String, templateId: String? = null) {
        val trimmed = textToSend.trim()
        if (trimmed.isEmpty() && templateId.isNullOrEmpty()) return
        if (isSending) return

        isSending = true
        errorMessage = null

        // Optimistic user message append
        val optimisticUserMsg = mapOf(
            "id" to "temp-${System.currentTimeMillis()}",
            "role" to "user",
            "content" to (if (trimmed.isNotEmpty()) trimmed else "/[Template Selected]"),
            "template_id" to templateId,
            "created_at" to null
        )
        messages = messages + optimisticUserMsg
        inputText = ""

        scope.launch {
            if (messages.isNotEmpty()) {
                listState.animateScrollToItem(messages.size - 1)
            }

            suspend fun legacySend() = if (mode == "session" && !recordingId.isNullOrBlank()) {
                apiClient.sendSessionChat(recordingId, trimmed, templateId)
            } else {
                apiClient.sendAllSessionsChat(trimmed, templateId)
            }

            val res = if (useLegacy) {
                legacySend()
            } else {
                var id = convId
                var createError: Throwable? = null
                if (id == null) {
                    val created = apiClient.createConversation(mode, if (mode == "session") recordingId else null)
                    id = created.getOrNull()?.get("id")?.toString()
                    convId = id
                    createError = created.exceptionOrNull()
                    if (id == null && isMissingEndpoint(createError)) useLegacy = true
                }
                when {
                    useLegacy -> legacySend()
                    id != null -> apiClient.sendConversationMessage(id, trimmed, templateId).also {
                        if (convTitle.isNullOrBlank() && trimmed.isNotEmpty()) convTitle = trimmed.take(60)
                    }
                    else -> Result.failure(createError ?: Exception("Could not start a conversation"))
                }
            }

            isSending = false
            if (res.isSuccess) {
                val asstMsg = res.getOrNull()
                if (asstMsg != null) {
                    messages = messages + asstMsg
                    keyboard?.hide()
                    focusManager.clearFocus()
                    scope.launch {
                        listState.animateScrollToItem(messages.size - 1)
                    }
                } else {
                    loadHistory()
                }
            } else {
                errorMessage = res.exceptionOrNull()?.message ?: "Failed to get AI response"
            }
        }
    }

    LaunchedEffect(recordingId, mode) {
        loadHistory()
        loadTemplates()
    }

    val colors = MaterialTheme.colorScheme
    val ext = LocalExtendedColors.current

    SlideBackContainer(onBack = onBack) {
        Scaffold(
            containerColor = colors.background,
            topBar = {
                Surface(
                    color = colors.surface,
                    border = BorderStroke(1.dp, colors.outline)
                ) {
                    Row(
                        modifier = Modifier
                            .fillMaxWidth()
                            .padding(horizontal = 16.dp, vertical = 12.dp),
                        verticalAlignment = Alignment.CenterVertically
                    ) {
                        IconButton(
                            onClick = onBack,
                            modifier = Modifier
                                .size(40.dp)
                                .clip(CircleShape)
                                .background(colors.surfaceVariant)
                        ) {
                            Text("←", color = colors.onBackground, fontSize = 20.sp, fontWeight = FontWeight.Bold)
                        }

                        Spacer(modifier = Modifier.width(12.dp))

                        Column(modifier = Modifier.weight(1f)) {
                            Text(
                                text = headerTitle,
                                color = colors.onBackground,
                                fontSize = 16.sp,
                                fontWeight = FontWeight.Bold,
                                maxLines = 1,
                                overflow = TextOverflow.Ellipsis
                            )
                            Text(
                                text = headerSubtitle,
                                color = CyanAccent,
                                fontSize = 12.sp,
                                fontWeight = FontWeight.Normal
                            )
                        }

                        IconButton(
                            onClick = { loadHistory() },
                            modifier = Modifier
                                .size(36.dp)
                                .clip(CircleShape)
                                .background(colors.surfaceVariant)
                        ) {
                            Text("🔄", fontSize = 14.sp)
                        }
                    }
                }
            },
            bottomBar = {
                // Bottom Input Row
                Surface(
                    color = colors.surface,
                    border = BorderStroke(1.dp, colors.outline),
                    modifier = Modifier.imePadding()
                ) {
                    Column(
                        modifier = Modifier
                            .fillMaxWidth()
                            .padding(horizontal = 12.dp, vertical = 10.dp)
                    ) {
                        // Quick Action Chips Row
                        Row(
                            modifier = Modifier
                                .fillMaxWidth()
                                .horizontalScroll(rememberScrollState())
                                .padding(bottom = 8.dp),
                            horizontalArrangement = Arrangement.spacedBy(8.dp)
                        ) {
                            // "/ Templates" chip
                            Surface(
                                shape = RoundedCornerShape(16.dp),
                                color = CyanAccent.copy(alpha = 0.15f),
                                border = BorderStroke(1.dp, CyanAccent),
                                modifier = Modifier.clickable {
                                    loadTemplates()
                                    showTemplateSheet = true
                                }
                            ) {
                                Row(
                                    modifier = Modifier.padding(horizontal = 12.dp, vertical = 6.dp),
                                    verticalAlignment = Alignment.CenterVertically
                                ) {
                                    Text("⚡", fontSize = 12.sp)
                                    Spacer(modifier = Modifier.width(4.dp))
                                    Text(
                                        "/ Templates",
                                        color = CyanAccent,
                                        fontSize = 12.sp,
                                        fontWeight = FontWeight.Bold
                                    )
                                }
                            }

                        }

                        // Message Input Field and Send Button
                        Row(
                            modifier = Modifier.fillMaxWidth(),
                            verticalAlignment = Alignment.CenterVertically
                        ) {
                            OutlinedTextField(
                                value = inputText,
                                onValueChange = { newText ->
                                    inputText = newText
                                    // Trigger template picker if user types "/"
                                    if (newText.endsWith("/") || newText == "/") {
                                        loadTemplates()
                                        showTemplateSheet = true
                                    }
                                },
                                placeholder = {
                                    Text(
                                        text = if (mode == "session") "Ask anything about this recording..." else "Ask across all sessions... (type / for templates)",
                                        color = ext.textMuted,
                                        fontSize = 13.sp
                                    )
                                },
                                modifier = Modifier
                                    .weight(1f)
                                    .defaultMinSize(minHeight = 44.dp),
                                maxLines = 4,
                                shape = RoundedCornerShape(22.dp),
                                colors = OutlinedTextFieldDefaults.colors(
                                    focusedContainerColor = colors.surfaceVariant,
                                    unfocusedContainerColor = colors.surfaceVariant,
                                    focusedBorderColor = CyanAccent,
                                    unfocusedBorderColor = colors.outline,
                                    focusedTextColor = colors.onBackground,
                                    unfocusedTextColor = colors.onBackground
                                )
                            )

                            Spacer(modifier = Modifier.width(8.dp))

                            IconButton(
                                onClick = { sendMessage(inputText) },
                                enabled = (inputText.isNotBlank() && !isSending),
                                modifier = Modifier
                                    .size(44.dp)
                                    .clip(CircleShape)
                                    .background(if (inputText.isNotBlank() && !isSending) EmeraldPrimary else colors.surfaceVariant)
                            ) {
                                Text(
                                    text = "➤",
                                    color = if (inputText.isNotBlank() && !isSending) colors.onBackground else ext.textMuted,
                                    fontSize = 18.sp,
                                    fontWeight = FontWeight.Bold
                                )
                            }
                        }
                    }
                }
            }
        ) { paddingValues ->
            Box(
                modifier = Modifier
                    .fillMaxSize()
                    .padding(paddingValues)
            ) {
                if (isLoadingHistory && messages.isEmpty()) {
                    Box(
                        modifier = Modifier.fillMaxSize(),
                        contentAlignment = Alignment.Center
                    ) {
                        Column(horizontalAlignment = Alignment.CenterHorizontally) {
                            CircularProgressIndicator(color = CyanAccent, modifier = Modifier.size(32.dp))
                            Spacer(modifier = Modifier.height(12.dp))
                            Text("Loading chat history...", color = colors.onSurfaceVariant, fontSize = 13.sp)
                        }
                    }
                } else if (messages.isEmpty()) {
                    // Empty state
                    Box(
                        modifier = Modifier
                            .fillMaxSize()
                            .padding(24.dp),
                        contentAlignment = Alignment.Center
                    ) {
                        Column(
                            horizontalAlignment = Alignment.CenterHorizontally,
                            verticalArrangement = Arrangement.Center
                        ) {
                            Text("🤖", fontSize = 48.sp)
                            Spacer(modifier = Modifier.height(16.dp))
                            Text(
                                text = if (mode == "session") "Ask Anything About This Session" else "Organization-Wide AI Chat",
                                color = colors.onBackground,
                                fontSize = 18.sp,
                                fontWeight = FontWeight.Bold,
                                textAlign = androidx.compose.ui.text.style.TextAlign.Center
                            )
                            Spacer(modifier = Modifier.height(8.dp))
                            Text(
                                text = if (mode == "session") {
                                    "Ask for summaries, action items, who said what, or use / templates for structured reports."
                                } else {
                                    "Search, compare, and query across all recording sessions in the organization."
                                },
                                color = ext.textMuted,
                                fontSize = 13.sp,
                                textAlign = androidx.compose.ui.text.style.TextAlign.Center
                            )
                            Spacer(modifier = Modifier.height(20.dp))
                            Button(
                                onClick = {
                                    loadTemplates()
                                    showTemplateSheet = true
                                },
                                colors = ButtonDefaults.buttonColors(containerColor = CyanAccent.copy(alpha = 0.2f)),
                                border = BorderStroke(1.dp, CyanAccent),
                                shape = RoundedCornerShape(20.dp)
                            ) {
                                Text("⚡ Browse / Templates", color = CyanAccent, fontWeight = FontWeight.Bold)
                            }
                        }
                    }
                } else {
                    LazyColumn(
                        state = listState,
                        modifier = Modifier
                            .fillMaxSize()
                            .padding(horizontal = 12.dp, vertical = 8.dp),
                        verticalArrangement = Arrangement.spacedBy(10.dp)
                    ) {
                        items(messages) { msg ->
                            val isUser = (msg["role"] as? String) == "user"
                            val rawContent = msg["content"] as? String ?: ""
                            val content = sanitizeDisplayContent(rawContent)
                            val tmplId = msg["template_id"] as? String

                            Row(
                                modifier = Modifier.fillMaxWidth(),
                                horizontalArrangement = if (isUser) Arrangement.End else Arrangement.Start
                            ) {
                                if (isUser) {
                                    // User message bubble
                                    Surface(
                                        shape = RoundedCornerShape(topStart = 16.dp, topEnd = 16.dp, bottomStart = 16.dp, bottomEnd = 4.dp),
                                        color = colors.surfaceVariant,
                                        border = BorderStroke(1.dp, CyanAccent.copy(alpha = 0.3f)),
                                        modifier = Modifier.fillMaxWidth(0.8f)
                                    ) {
                                        Column(modifier = Modifier.padding(horizontal = 14.dp, vertical = 10.dp)) {
                                            if (!tmplId.isNullOrEmpty()) {
                                                Surface(
                                                    shape = RoundedCornerShape(6.dp),
                                                    color = CyanAccent.copy(alpha = 0.2f),
                                                    modifier = Modifier.padding(bottom = 6.dp)
                                                ) {
                                                    Text(
                                                        text = "⚡ Template Command",
                                                        color = CyanAccent,
                                                        fontSize = 10.sp,
                                                        fontWeight = FontWeight.Bold,
                                                        modifier = Modifier.padding(horizontal = 6.dp, vertical = 2.dp)
                                                    )
                                                }
                                            }
                                            SelectionContainer {
                                                Text(
                                                    text = content,
                                                    color = colors.onBackground,
                                                    fontSize = 14.sp,
                                                    lineHeight = 20.sp
                                                )
                                            }
                                            Spacer(modifier = Modifier.height(4.dp))
                                            Row(
                                                modifier = Modifier.fillMaxWidth(),
                                                horizontalArrangement = Arrangement.End,
                                                verticalAlignment = Alignment.CenterVertically
                                            ) {
                                                IconButton(
                                                    onClick = { copyToClipboard(content) },
                                                    modifier = Modifier.size(28.dp)
                                                ) {
                                                    Text("📋", fontSize = 13.sp)
                                                }
                                                Spacer(modifier = Modifier.width(4.dp))
                                                IconButton(
                                                    onClick = { shareText(content) },
                                                    modifier = Modifier.size(28.dp)
                                                ) {
                                                    Text("↗️", fontSize = 13.sp)
                                                }
                                            }
                                        }
                                    }
                                } else {
                                    // Assistant message bubble
                                    Surface(
                                        shape = RoundedCornerShape(topStart = 16.dp, topEnd = 16.dp, bottomStart = 4.dp, bottomEnd = 16.dp),
                                        color = colors.surface,
                                        border = BorderStroke(1.dp, colors.outline),
                                        modifier = Modifier.fillMaxWidth(0.85f)
                                    ) {
                                        Column(modifier = Modifier.padding(14.dp)) {
                                            Row(
                                                modifier = Modifier.fillMaxWidth(),
                                                verticalAlignment = Alignment.CenterVertically
                                            ) {
                                                Text("✨", fontSize = 12.sp)
                                                Spacer(modifier = Modifier.width(4.dp))
                                                Text(
                                                    "Gemini AI",
                                                    color = CyanAccent,
                                                    fontSize = 12.sp,
                                                    fontWeight = FontWeight.Bold
                                                )
                                            }

                                            Spacer(modifier = Modifier.height(6.dp))

                                            SelectionContainer {
                                                Text(
                                                    text = content,
                                                    color = colors.onBackground,
                                                    fontSize = 14.sp,
                                                    lineHeight = 21.sp
                                                )
                                            }

                                            Spacer(modifier = Modifier.height(8.dp))

                                            // Action icons at bottom: Copy and Share
                                            Row(
                                                modifier = Modifier.fillMaxWidth(),
                                                horizontalArrangement = Arrangement.End,
                                                verticalAlignment = Alignment.CenterVertically
                                            ) {
                                                IconButton(
                                                    onClick = { copyToClipboard(content) },
                                                    modifier = Modifier.size(32.dp)
                                                ) {
                                                    Text("📋", fontSize = 15.sp)
                                                }
                                                Spacer(modifier = Modifier.width(6.dp))
                                                IconButton(
                                                    onClick = { shareText(content) },
                                                    modifier = Modifier.size(32.dp)
                                                ) {
                                                    Text("↗️", fontSize = 15.sp)
                                                }
                                            }
                                        }
                                    }
                                }
                            }
                        }

                        // Thinking indicator bubble
                        if (isSending) {
                            item {
                                Row(
                                    modifier = Modifier.fillMaxWidth(),
                                    horizontalArrangement = Arrangement.Start
                                ) {
                                    Surface(
                                        shape = RoundedCornerShape(16.dp),
                                        color = colors.surface,
                                        border = BorderStroke(1.dp, colors.outline),
                                        modifier = Modifier.padding(top = 4.dp)
                                    ) {
                                        Row(
                                            modifier = Modifier.padding(horizontal = 14.dp, vertical = 10.dp),
                                            verticalAlignment = Alignment.CenterVertically
                                        ) {
                                            CircularProgressIndicator(
                                                color = CyanAccent,
                                                modifier = Modifier.size(16.dp),
                                                strokeWidth = 2.dp
                                            )
                                            Spacer(modifier = Modifier.width(10.dp))
                                            Text(
                                                text = "Gemini is analyzing...",
                                                color = colors.onSurfaceVariant,
                                                fontSize = 13.sp
                                            )
                                        }
                                    }
                                }
                            }
                        }
                    }
                }

                // Error Banner
                if (errorMessage != null) {
                    Surface(
                        color = RecordRed.copy(alpha = 0.9f),
                        shape = RoundedCornerShape(8.dp),
                        modifier = Modifier
                            .align(Alignment.TopCenter)
                            .padding(12.dp)
                            .fillMaxWidth()
                    ) {
                        Row(
                            modifier = Modifier.padding(horizontal = 12.dp, vertical = 8.dp),
                            verticalAlignment = Alignment.CenterVertically
                        ) {
                            Text("⚠️", fontSize = 14.sp)
                            Spacer(modifier = Modifier.width(8.dp))
                            Text(
                                text = errorMessage ?: "",
                                color = colors.onBackground,
                                fontSize = 12.sp,
                                modifier = Modifier.weight(1f)
                            )
                            IconButton(
                                onClick = { errorMessage = null },
                                modifier = Modifier.size(20.dp)
                            ) {
                                Text("✕", color = colors.onBackground, fontSize = 12.sp)
                            }
                        }
                    }
                }
            }
        }

        // ── Templates Bottom Sheet ──────────────────────────────────────────────────
        if (showTemplateSheet) {
            ModalBottomSheet(
                onDismissRequest = { showTemplateSheet = false },
                containerColor = colors.surface,
                scrimColor = Color.Black.copy(alpha = 0.6f),
                dragHandle = {
                    Box(
                        modifier = Modifier
                            .padding(vertical = 10.dp)
                            .width(36.dp)
                            .height(4.dp)
                            .background(colors.outline, RoundedCornerShape(2.dp))
                    )
                }
            ) {
                Column(
                    modifier = Modifier
                        .fillMaxWidth()
                        .padding(horizontal = 16.dp, vertical = 8.dp)
                        .padding(bottom = 24.dp)
                ) {
                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        horizontalArrangement = Arrangement.SpaceBetween,
                        verticalAlignment = Alignment.CenterVertically
                    ) {
                        Column {
                            Text(
                                text = "Prompt Templates (/) ",
                                color = colors.onBackground,
                                fontSize = 18.sp,
                                fontWeight = FontWeight.Bold
                            )
                            Text(
                                text = "Select a template to generate structured AI analysis",
                                color = ext.textMuted,
                                fontSize = 12.sp
                            )
                        }

                        IconButton(onClick = { showTemplateSheet = false }) {
                            Text("✕", color = colors.onSurfaceVariant, fontSize = 16.sp)
                        }
                    }

                    Spacer(modifier = Modifier.height(14.dp))

                    if (isLoadingTemplates && templates.isEmpty()) {
                        Box(
                            modifier = Modifier
                                .fillMaxWidth()
                                .height(120.dp),
                            contentAlignment = Alignment.Center
                        ) {
                            CircularProgressIndicator(color = CyanAccent, modifier = Modifier.size(24.dp))
                        }
                    } else if (templates.isEmpty()) {
                        Text(
                            text = "No prompt templates configured. Admin can add templates in Settings.",
                            color = ext.textMuted,
                            fontSize = 13.sp,
                            modifier = Modifier.padding(vertical = 16.dp)
                        )
                    } else {
                        Column(
                            modifier = Modifier
                                .fillMaxWidth()
                                .heightIn(max = 400.dp)
                                .verticalScroll(rememberScrollState()),
                            verticalArrangement = Arrangement.spacedBy(10.dp)
                        ) {
                            templates.forEach { tmpl ->
                                val tid = tmpl["id"] as? String ?: ""
                                val title = tmpl["title"] as? String ?: "Template"
                                val icon = tmpl["icon"] as? String ?: "📋"
                                val prompt = tmpl["prompt"] as? String ?: ""
                                val example = tmpl["example_output"] as? String

                                Surface(
                                    shape = RoundedCornerShape(12.dp),
                                    color = colors.surfaceVariant,
                                    border = BorderStroke(1.dp, colors.outline),
                                    modifier = Modifier
                                        .fillMaxWidth()
                                        .clickable {
                                            showTemplateSheet = false
                                            sendMessage(inputText, tid)
                                        }
                                ) {
                                    Column(modifier = Modifier.padding(14.dp)) {
                                        Row(
                                            verticalAlignment = Alignment.CenterVertically,
                                            modifier = Modifier.fillMaxWidth()
                                        ) {
                                            Text(icon, fontSize = 20.sp)
                                            Spacer(modifier = Modifier.width(10.dp))
                                            Text(
                                                text = title,
                                                color = colors.onBackground,
                                                fontSize = 15.sp,
                                                fontWeight = FontWeight.Bold,
                                                modifier = Modifier.weight(1f)
                                            )
                                            Text(
                                                text = "Apply ➔",
                                                color = CyanAccent,
                                                fontSize = 12.sp,
                                                fontWeight = FontWeight.SemiBold
                                            )
                                        }

                                        Spacer(modifier = Modifier.height(6.dp))

                                        Text(
                                            text = prompt,
                                            color = colors.onSurfaceVariant,
                                            fontSize = 12.sp,
                                            maxLines = 2,
                                            overflow = TextOverflow.Ellipsis
                                        )

                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
    }
}

private fun sanitizeDisplayContent(text: String): String {
    if (text.isEmpty()) return text
    var clean = text.trim()

    // 1. Check if contains markdown json or raw json
    if (clean.contains("```json") || clean.contains("```") || clean.startsWith("{")) {
        try {
            val jsonPattern = Regex("""```(?:json)?\s*([\s\S]*?)(?:```|$)""")
            val match = jsonPattern.find(clean)
            val jsonCandidate = match?.groupValues?.get(1)?.trim() ?: clean

            var extracted: String? = null
            // Try parsing with Gson first if valid JSON
            try {
                val mapType = object : com.google.gson.reflect.TypeToken<Map<String, Any?>>() {}.type
                val map: Map<String, Any?> = com.google.gson.Gson().fromJson(jsonCandidate, mapType)
                val title = map["title"] as? String
                val body = (map["overview"] ?: map["report"] ?: map["summary"] ?: map["content"] ?: map["response"] ?: map["message"]) as? String
                if (!body.isNullOrBlank()) {
                    val header = if (!title.isNullOrBlank() && !title.startsWith("Untitled", ignoreCase = true)) "### $title\n\n" else ""
                    extracted = header + body
                }
            } catch (_: Exception) {}

            // Fallback regex extraction if Gson failed (e.g. truncated or unescaped quotes/newlines inside JSON)
            if (extracted == null) {
                val titlePattern = Regex(""""title"\s*:\s*"([^"\n\r]+)"""")
                val titleMatch = titlePattern.find(jsonCandidate)
                val title = titleMatch?.groupValues?.get(1)

                val bodyPattern = Regex(""""(?:overview|report|summary|content|response|message)"\s*:\s*"([\s\S]*?)(?:"\s*,\s*"\w+"|\s*"\s*\}|\s*"$|$)""")
                val bodyMatch = bodyPattern.find(jsonCandidate)
                var body = bodyMatch?.groupValues?.get(1)?.trim()

                if (!body.isNullOrBlank()) {
                    body = body.replace(Regex("""["'}\s]+$"""), "")
                    val header = if (!title.isNullOrBlank() && !title.startsWith("Untitled", ignoreCase = true)) "### $title\n\n" else ""
                    extracted = header + body
                } else if (!title.isNullOrBlank()) {
                    extracted = "### $title"
                }
            }

            if (extracted != null) {
                val prefix = if (match != null && match.range.first > 0) clean.substring(0, match.range.first).trim() else ""
                val combined = if (prefix.isNotEmpty()) "$prefix\n\n$extracted" else extracted
                return combined.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\t", "\t")
            }
        } catch (_: Exception) {}
    }

    // 2. Regular text unescaping
    if (clean.contains("\\n")) {
        clean = clean.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\t", "\t")
    }

    return clean
}
