package com.isf.recorder.ui.screens

import android.net.Uri
import android.provider.OpenableColumns
import android.widget.Toast
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.work.Constraints
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.WorkManager
import com.isf.recorder.data.api.ISFApiClient
import com.isf.recorder.data.prefs.UserPreferences
import com.isf.recorder.ui.components.*
import com.isf.recorder.ui.theme.*
import kotlinx.coroutines.async
import kotlinx.coroutines.launch

@Composable
fun SettingsScreen(
    onLogout: () -> Unit,
    onBack: () -> Unit,
    onAskJomi: () -> Unit = {},
    onSwitchView: () -> Unit = {}
) {
    val context = LocalContext.current
    val prefs = remember { UserPreferences(context) }
    val apiClient = remember { ISFApiClient(context) }
    val workManager = remember { WorkManager.getInstance(context) }
    val scope = rememberCoroutineScope()

    var syncTriggered by remember { mutableStateOf(false) }

    // Team members management state
    var teamMembers by remember { mutableStateOf<List<Map<String, Any?>>>(emptyList()) }
    var isLoadingMembers by remember { mutableStateOf(false) }
    var showAddDialog by remember { mutableStateOf(false) }
    var editingMember by remember { mutableStateOf<Map<String, Any?>?>(null) }

    // Add dialog fields
    var addName by remember { mutableStateOf("") }
    var addPhone by remember { mutableStateOf("") }
    var addRole by remember { mutableStateOf("staff") }
    var isAddingUser by remember { mutableStateOf(false) }
    var addError by remember { mutableStateOf<String?>(null) }

    // Edit dialog fields
    var editName by remember { mutableStateOf("") }
    var editRole by remember { mutableStateOf("staff") }
    var isEditingUser by remember { mutableStateOf(false) }
    var editError by remember { mutableStateOf<String?>(null) }

    // AI Chat Templates state (Admin only)
    var chatTemplates by remember { mutableStateOf<List<Map<String, Any?>>>(emptyList()) }
    var isLoadingTemplates by remember { mutableStateOf(false) }
    var showTemplateDialog by remember { mutableStateOf(false) }
    var editingTemplateId by remember { mutableStateOf<String?>(null) }
    var tmplTitle by remember { mutableStateOf("") }
    var tmplPrompt by remember { mutableStateOf("") }
    var tmplExample by remember { mutableStateOf("") }
    var tmplIcon by remember { mutableStateOf("📋") }
    var isSavingTemplate by remember { mutableStateOf(false) }
    var templateError by remember { mutableStateOf<String?>(null) }

    // AI Model Configuration state
    var aiModelConfig by remember { mutableStateOf<Map<String, Any?>?>(null) }
    var availableModels by remember { mutableStateOf<List<Map<String, Any?>>>(emptyList()) }
    var isLoadingModels by remember { mutableStateOf(false) }
    var selectedTranscriptionModel by remember { mutableStateOf("") }
    var selectedIntelligenceModel by remember { mutableStateOf("") }
    var isSavingModel by remember { mutableStateOf(false) }
    var modelSaveMessage by remember { mutableStateOf<String?>(null) }
    var useSameModelForBoth by remember { mutableStateOf(true) }

    // Upload audio to test a model (Admin only)
    var importUri by remember { mutableStateOf<Uri?>(null) }
    var importFileName by remember { mutableStateOf("") }
    var importFileSize by remember { mutableLongStateOf(0L) }
    var importModel by remember { mutableStateOf("gemini-2.5-flash") }
    var importTitle by remember { mutableStateOf("") }
    var importProgress by remember { mutableStateOf<Float?>(null) }
    var importMessage by remember { mutableStateOf<String?>(null) }
    val pickAudioLauncher = rememberLauncherForActivityResult(ActivityResultContracts.OpenDocument()) { uri ->
        if (uri != null) {
            importUri = uri
            importMessage = null
            importFileName = "audio"
            importFileSize = 0L
            context.contentResolver.query(uri, null, null, null, null)?.use { c ->
                val nameIdx = c.getColumnIndex(OpenableColumns.DISPLAY_NAME)
                val sizeIdx = c.getColumnIndex(OpenableColumns.SIZE)
                if (c.moveToFirst()) {
                    if (nameIdx >= 0) importFileName = c.getString(nameIdx) ?: "audio"
                    if (sizeIdx >= 0 && !c.isNull(sizeIdx)) importFileSize = c.getLong(sizeIdx)
                }
            }
            if (importTitle.isBlank()) importTitle = importFileName.substringBeforeLast('.')
        }
    }

    fun loadMembers() {
        if (!prefs.isAdmin) return
        isLoadingMembers = true
        scope.launch {
            try {
                val res = apiClient.listUsers()
                isLoadingMembers = false
                if (res.isSuccess) {
                    teamMembers = res.getOrNull() ?: emptyList()
                }
            } catch (t: Throwable) {
                isLoadingMembers = false
            }
        }
    }

    var templatesVersion by remember { mutableIntStateOf(0) }

    fun loadChatTemplates() {
        if (!prefs.isAdmin) return
        templatesVersion++          // the Reports card reloads its template list too
        isLoadingTemplates = true
        scope.launch {
            val res = apiClient.listChatTemplates()
            isLoadingTemplates = false
            if (res.isSuccess) {
                chatTemplates = res.getOrNull() ?: emptyList()
            }
        }
    }

    LaunchedEffect(Unit) {
        loadChatTemplates()
        // Load AI model config for all users (staff sees read-only, admin can edit)
        isLoadingModels = true
        scope.launch {
            val configDeferred = async { apiClient.getAIModelConfig() }
            val modelsDeferred = if (prefs.isAdmin) async { apiClient.getAvailableModels() } else null

            val configRes = configDeferred.await()
            if (configRes.isSuccess) {
                aiModelConfig = configRes.getOrNull()
                selectedTranscriptionModel = aiModelConfig?.get("transcription_model")?.toString() ?: "gemini-2.5-flash"
                selectedIntelligenceModel = aiModelConfig?.get("intelligence_model")?.toString() ?: "gemini-2.5-flash"
                useSameModelForBoth = selectedTranscriptionModel == selectedIntelligenceModel
            }
            if (modelsDeferred != null) {
                val modelsRes = modelsDeferred.await()
                if (modelsRes.isSuccess) {
                    availableModels = modelsRes.getOrNull() ?: emptyList()
                }
            }
            isLoadingModels = false
        }
    }

    val colors = MaterialTheme.colorScheme
    val ext = LocalExtendedColors.current

    var openSection by remember { mutableStateOf<String?>(null) }
    var teamSummary by remember { mutableStateOf("") }
    var listsSummary by remember { mutableStateOf("") }
    var reportsSummary by remember { mutableStateOf("") }
    var unfinishedSummary by remember { mutableStateOf("") }
    LaunchedEffect(Unit) {
        if (!prefs.isAdmin) return@LaunchedEffect
        apiClient.listUsers().onSuccess { teamSummary = "${it.size} people" }
        val b = apiClient.listListItems("balgruh", includeInactive = false).getOrNull()?.size
        val c = apiClient.listListItems("coach_name", includeInactive = false).getOrNull()?.size
        if (b != null) listsSummary = "$b Balgruha · ${c ?: 0} names"
        apiClient.getRecorderConfig().onSuccess { cfg ->
            val t = cfg["report_templates"] as? Map<*, *>
            fun title(k: String) = ((t?.get(k) as? Map<*, *>)?.get("title")?.toString() ?: "—")
                .replace("Report template for Coach", "Coach")
            reportsSummary = "${title("schedule")} / ${title("meeting")}"
        }
        apiClient.listUnfinishedSessions().onSuccess { unfinishedSummary = if (it.isEmpty()) "None" else "${it.size}" }
    }
    val modelSummary = availableModels.firstOrNull { it["id"] == selectedTranscriptionModel }
        ?.let { it["short_name"]?.toString() ?: it["name"]?.toString() } ?: selectedTranscriptionModel

    SlideBackContainer(onBack = onBack) {
        // Phone Back: first closes the open section (e.g. Lists); with none open, the container goes to Home.
        // Declared inside the container so it wins over the container's own Back handler.
        androidx.activity.compose.BackHandler(enabled = openSection != null) { openSection = null }
        Column(
            modifier = Modifier
                .fillMaxSize()
                .background(colors.background)
                .verticalScroll(rememberScrollState())
                .padding(16.dp)
        ) {
            BackHeader(title = "Settings", subtitle = prefs.userName, onBack = onBack)
            Spacer(modifier = Modifier.height(16.dp))

            // ── v2.1: a short menu of collapsible sections; one open at a time, all closed at first ──
            fun toggle(id: String) { openSection = if (openSection == id) null else id }

            SettingsSection("👤", "Profile", "${prefs.userName ?: ""} · ${prefs.displayRole}", open = true, onToggle = {}, alwaysOpen = true) {
                ProfileCard(prefs = prefs, onSwitchView = onSwitchView, onSignOut = onLogout)
            }
            SettingsSection("💬", "Ask Jomi", "Saved conversations", open = false, onToggle = onAskJomi, navigates = true)
            SettingsSection("📞", "Calls", if (prefs.callSyncOn) "Syncing your call recordings" else "Off",
                openSection == "calls", { toggle("calls") }) {
                CallsSettingsSection(apiClient)
            }
            SettingsSection("🔔", "Notifications", if (prefs.isSuperAdmin) "Your alerts · send a message to the team" else "Choose what you get",
                openSection == "notifications", { toggle("notifications") }) {
                NotificationsSettingsSection(apiClient)
            }

            if (prefs.isAdmin) {
                SettingsSection("👥", "Team", teamSummary, openSection == "team", { toggle("team") }) {
                    TeamSection(apiClient, prefs)
                }
                SettingsSection("📋", "Lists", listsSummary, openSection == "lists", { toggle("lists") }) {
                    ListsSection(apiClient, prefs.roleLabel)
                }
                SettingsSection("📝", "Reports", reportsSummary, openSection == "reports", { toggle("reports") }) {
                    ReportTemplatesSection(apiClient, refreshKey = templatesVersion, onTemplatesChanged = { loadChatTemplates() })
                }
                SettingsSection("⏱", "Silence auto-stop", "Stop a recording when nobody speaks", openSection == "silence", { toggle("silence") }) {
                    SilenceStopSection(apiClient)
                }
                SettingsSection("🗂", "AI chat templates", "${chatTemplates.size} templates", openSection == "templates", { toggle("templates") }) {
                    // ── AI Chat Templates Management (Admin Only) ─────────────────────────
                    Surface(
                        color = colors.surface,
                        shape = RoundedCornerShape(14.dp),
                        border = BorderStroke(1.dp, colors.outline),
                        modifier = Modifier.fillMaxWidth()
                    ) {
                        Column(modifier = Modifier.padding(16.dp)) {
                            Row(
                                modifier = Modifier.fillMaxWidth(),
                                horizontalArrangement = Arrangement.SpaceBetween,
                                verticalAlignment = Alignment.CenterVertically
                            ) {
                                Column(modifier = Modifier.weight(1f).padding(end = 8.dp)) {
                                    Text("AI CHAT TEMPLATES", color = CyanAccent, fontSize = 11.sp, fontWeight = FontWeight.Bold)
                                    Text("Prompt templates accessed via / in chat", color = ext.textMuted, fontSize = 12.sp)
                                }

                                Button(
                                    onClick = {
                                        editingTemplateId = null
                                        tmplTitle = ""
                                        tmplPrompt = ""
                                        tmplExample = ""
                                        tmplIcon = "📋"
                                        templateError = null
                                        showTemplateDialog = true
                                    },
                                    colors = ButtonDefaults.buttonColors(containerColor = CyanAccent.copy(alpha = 0.2f)),
                                    border = BorderStroke(1.dp, CyanAccent),
                                    shape = RoundedCornerShape(16.dp),
                                    contentPadding = PaddingValues(horizontal = 10.dp, vertical = 4.dp)
                                ) {
                                    Text("+ Add Template", color = CyanAccent, fontSize = 12.sp, fontWeight = FontWeight.Bold)
                                }
                            }

                            Spacer(modifier = Modifier.height(12.dp))

                            if (isLoadingTemplates && chatTemplates.isEmpty()) {
                                Box(
                                    modifier = Modifier.fillMaxWidth().height(60.dp),
                                    contentAlignment = Alignment.Center
                                ) {
                                    CircularProgressIndicator(color = CyanAccent, modifier = Modifier.size(20.dp))
                                }
                            } else if (chatTemplates.isEmpty()) {
                                Text("No templates found. Tap '+ Add Template' to create one.", color = ext.textMuted, fontSize = 13.sp)
                            } else {
                                Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                                    chatTemplates.forEach { tmpl ->
                                        val tid = tmpl["id"] as? String ?: ""
                                        val title = tmpl["title"] as? String ?: ""
                                        val icon = tmpl["icon"] as? String ?: "📋"
                                        val prompt = tmpl["prompt"] as? String ?: ""
                                        val example = tmpl["example_output"] as? String

                                        Surface(
                                            color = colors.surfaceVariant,
                                            shape = RoundedCornerShape(10.dp),
                                            border = BorderStroke(1.dp, colors.outline),
                                            modifier = Modifier.fillMaxWidth()
                                        ) {
                                            Row(
                                                modifier = Modifier.padding(12.dp),
                                                verticalAlignment = Alignment.CenterVertically
                                            ) {
                                                Text(icon, fontSize = 20.sp)
                                                Spacer(modifier = Modifier.width(10.dp))
                                                Column(modifier = Modifier.weight(1f)) {
                                                    Text(title, color = colors.onBackground, fontSize = 14.sp, fontWeight = FontWeight.Bold)
                                                    Text(
                                                        prompt,
                                                        color = colors.onSurfaceVariant,
                                                        fontSize = 11.sp,
                                                        maxLines = 1,
                                                        overflow = androidx.compose.ui.text.style.TextOverflow.Ellipsis
                                                    )
                                                }

                                                // Edit Button
                                                IconButton(
                                                    onClick = {
                                                        editingTemplateId = tid
                                                        tmplTitle = title
                                                        tmplPrompt = prompt
                                                        tmplExample = example ?: ""
                                                        tmplIcon = icon
                                                        templateError = null
                                                        showTemplateDialog = true
                                                    },
                                                    modifier = Modifier.size(32.dp)
                                                ) {
                                                    Text("✏️", fontSize = 14.sp)
                                                }

                                                // Delete Button
                                                IconButton(
                                                    onClick = {
                                                        scope.launch {
                                                            val delRes = apiClient.deleteChatTemplate(tid)
                                                            if (delRes.isSuccess) {
                                                                Toast.makeText(context, "Template deleted", Toast.LENGTH_SHORT).show()
                                                                loadChatTemplates()
                                                            } else {
                                                                Toast.makeText(context, delRes.exceptionOrNull()?.message ?: "Could not delete the template", Toast.LENGTH_LONG).show()
                                                            }
                                                        }
                                                    },
                                                    modifier = Modifier.size(32.dp)
                                                ) {
                                                    Text("🗑️", fontSize = 14.sp)
                                                }
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
                SettingsSection("🤖", "AI model", modelSummary, openSection == "model", { toggle("model") }) {
                Surface(
                    color = colors.surface,
                    shape = RoundedCornerShape(14.dp),
                    border = BorderStroke(1.dp, Color(0xFFD4AF37).copy(alpha = 0.5f)),
                    modifier = Modifier.fillMaxWidth()
                ) {
                    Column(modifier = Modifier.padding(16.dp)) {
                        Row(
                            modifier = Modifier.fillMaxWidth(),
                            verticalAlignment = Alignment.CenterVertically
                        ) {
                            Text("⚡", fontSize = 16.sp)
                            Spacer(modifier = Modifier.width(6.dp))
                            Column(modifier = Modifier.weight(1f)) {
                                Text("AI MODEL CONFIGURATION", color = Color(0xFFD4AF37), fontSize = 11.sp, fontWeight = FontWeight.Bold)
                                Text(
                                    text = if (prefs.isAdmin) "Select which AI model powers transcription & chat" else "Currently active AI model",
                                    color = colors.onSurfaceVariant,
                                    fontSize = 12.sp
                                )
                            }
                        }

                        Spacer(modifier = Modifier.height(12.dp))

                        if (isLoadingModels) {
                            Box(
                                modifier = Modifier.fillMaxWidth().height(40.dp),
                                contentAlignment = Alignment.Center
                            ) {
                                CircularProgressIndicator(color = Color(0xFFD4AF37), modifier = Modifier.size(20.dp))
                            }
                        } else if (prefs.isAdmin && availableModels.isNotEmpty()) {
                            // Admin: Editable model selector
                            // "Use same model for both" toggle
                            Row(
                                modifier = Modifier.fillMaxWidth(),
                                verticalAlignment = Alignment.CenterVertically
                            ) {
                                Text("Use same model for everything", color = colors.onSurfaceVariant, fontSize = 12.sp, modifier = Modifier.weight(1f))
                                Switch(
                                    checked = useSameModelForBoth,
                                    onCheckedChange = {
                                        useSameModelForBoth = it
                                        if (it) {
                                            selectedIntelligenceModel = selectedTranscriptionModel
                                        }
                                    },
                                    colors = SwitchDefaults.colors(
                                        checkedThumbColor = Color(0xFFD4AF37),
                                        checkedTrackColor = Color(0xFFD4AF37).copy(alpha = 0.3f)
                                    )
                                )
                            }

                            Spacer(modifier = Modifier.height(10.dp))

                            // Transcription model selector
                            Text(
                                text = if (useSameModelForBoth) "AI Model (All Operations)" else "Transcription Model (STT)",
                                color = colors.onSurfaceVariant,
                                fontSize = 12.sp,
                                fontWeight = FontWeight.SemiBold
                            )
                            Spacer(modifier = Modifier.height(4.dp))
                            Column(verticalArrangement = Arrangement.spacedBy(6.dp)) {
                                availableModels.forEach { model ->
                                    val modelId = model["id"]?.toString() ?: ""
                                    val modelName = model["name"]?.toString() ?: modelId
                                    val modelDesc = model["description"]?.toString() ?: ""
                                    val category = model["category"]?.toString() ?: "fast"
                                    val provider = model["provider"]?.toString() ?: "Google"
                                    val isAssembly = provider.equals("AssemblyAI", ignoreCase = true)
                                    val isSelected = selectedTranscriptionModel == modelId
                                    ModelOptionRow(model, isSelected, accent = Color(0xFFB8860B), onSelect = {
                                        selectedTranscriptionModel = modelId
                                        if (useSameModelForBoth) {
                                            selectedIntelligenceModel = if (isAssembly || modelId.startsWith("openai-")) "gemini-2.5-flash" else modelId
                                        }
                                        modelSaveMessage = null
                                    })
                                }
                            }

                            // Intelligence model selector (only when not using same model)
                            if (!useSameModelForBoth) {
                                Spacer(modifier = Modifier.height(12.dp))
                                Text("Intelligence & Chat Model", color = colors.onSurfaceVariant, fontSize = 12.sp, fontWeight = FontWeight.SemiBold)
                                Spacer(modifier = Modifier.height(4.dp))
                                Column(verticalArrangement = Arrangement.spacedBy(6.dp)) {
                                    availableModels.filter {
                                        val supports = it["supports"] as? List<*>
                                        supports?.contains("intelligence") != false
                                    }.forEach { model ->
                                        val modelId = model["id"]?.toString() ?: ""
                                        val modelName = model["name"]?.toString() ?: modelId
                                        val isSelected = selectedIntelligenceModel == modelId
                                        ModelOptionRow(model, isSelected, accent = CyanAccent, onSelect = {
                                            selectedIntelligenceModel = modelId
                                            modelSaveMessage = null
                                        })
                                    }
                                }
                            }

                            // Info banner when AssemblyAI is chosen (explaining STT vs Intelligence)
                            if (selectedTranscriptionModel.contains("assemblyai", ignoreCase = true)) {
                                Surface(
                                    shape = RoundedCornerShape(10.dp),
                                    color = Color(0xFF8E24AA).copy(alpha = 0.15f),
                                    border = BorderStroke(1.dp, Color(0xFF8E24AA).copy(alpha = 0.4f)),
                                    modifier = Modifier.fillMaxWidth().padding(vertical = 4.dp)
                                ) {
                                    Row(
                                        modifier = Modifier.padding(10.dp),
                                        verticalAlignment = Alignment.CenterVertically
                                    ) {
                                        Text("🎙️", fontSize = 16.sp)
                                        Spacer(modifier = Modifier.width(8.dp))
                                        Column {
                                            Text(
                                                text = "AssemblyAI active for Speech-to-Text",
                                                color = Color(0xFFCE93D8),
                                                fontSize = 12.sp,
                                                fontWeight = FontWeight.Bold
                                            )
                                            Text(
                                                text = "Audio transcription uses AssemblyAI. Google Gemini ($selectedIntelligenceModel) powers Summaries & Chat.",
                                                color = colors.onSurfaceVariant,
                                                fontSize = 10.sp
                                            )
                                        }
                                    }
                                }
                            }

                            Spacer(modifier = Modifier.height(12.dp))

                            // Save Button
                            val hasChanges = selectedTranscriptionModel != (aiModelConfig?.get("transcription_model")?.toString() ?: "gemini-2.5-flash") ||
                                    selectedIntelligenceModel != (aiModelConfig?.get("intelligence_model")?.toString() ?: "gemini-2.5-flash")

                            Button(
                                onClick = {
                                    isSavingModel = true
                                    modelSaveMessage = null
                                    scope.launch {
                                        val res = apiClient.updateAIModel(
                                            transcriptionModel = selectedTranscriptionModel,
                                            intelligenceModel = selectedIntelligenceModel
                                        )
                                        isSavingModel = false
                                        if (res.isSuccess) {
                                            aiModelConfig = mapOf(
                                                "transcription_model" to selectedTranscriptionModel,
                                                "intelligence_model" to selectedIntelligenceModel
                                            )
                                            modelSaveMessage = "✅ Model updated! All users now use $selectedTranscriptionModel"
                                            Toast.makeText(context, "AI model updated for all users!", Toast.LENGTH_SHORT).show()
                                        } else {
                                            modelSaveMessage = "❌ ${res.exceptionOrNull()?.message ?: "Failed to update"}"
                                        }
                                    }
                                },
                                colors = ButtonDefaults.buttonColors(
                                    containerColor = if (hasChanges) Color(0xFFD4AF37) else colors.surfaceVariant
                                ),
                                shape = RoundedCornerShape(10.dp),
                                enabled = !isSavingModel && hasChanges,
                                modifier = Modifier.fillMaxWidth()
                            ) {
                                if (isSavingModel) {
                                    CircularProgressIndicator(modifier = Modifier.size(16.dp), color = ext.bgDark, strokeWidth = 2.dp)
                                    Spacer(modifier = Modifier.width(8.dp))
                                    Text("Saving...", color = ext.bgDark, fontWeight = FontWeight.Bold)
                                } else {
                                    Text(
                                        text = if (hasChanges) "💾 Save Model Change" else "No Changes",
                                        color = if (hasChanges) ext.bgDark else colors.onSurfaceVariant,
                                        fontWeight = FontWeight.Bold
                                    )
                                }
                            }

                            if (modelSaveMessage != null) {
                                Spacer(modifier = Modifier.height(6.dp))
                                Text(
                                    text = modelSaveMessage!!,
                                    color = if (modelSaveMessage!!.startsWith("✅")) EmeraldPrimary else Color(0xFFFF5252),
                                    fontSize = 12.sp,
                                    fontWeight = FontWeight.Medium
                                )
                            }
                        } else {
                            // Staff: Read-only display of current model
                            val currentSTT = aiModelConfig?.get("transcription_model_name")?.toString()
                                ?: aiModelConfig?.get("transcription_model")?.toString()
                                ?: "Loading..."
                            val currentIntel = aiModelConfig?.get("intelligence_model_name")?.toString()
                                ?: aiModelConfig?.get("intelligence_model")?.toString()
                                ?: "Gemini 2.5 Flash"
                            val isAssembly = currentSTT.contains("assemblyai", ignoreCase = true)

                            Surface(
                                shape = RoundedCornerShape(10.dp),
                                color = if (isAssembly) Color(0xFF8E24AA).copy(alpha = 0.1f) else Color(0xFFD4AF37).copy(alpha = 0.1f),
                                border = BorderStroke(1.dp, if (isAssembly) Color(0xFF8E24AA).copy(alpha = 0.35f) else Color(0xFFD4AF37).copy(alpha = 0.3f)),
                                modifier = Modifier.fillMaxWidth()
                            ) {
                                Row(
                                    modifier = Modifier.padding(horizontal = 14.dp, vertical = 10.dp),
                                    verticalAlignment = Alignment.CenterVertically
                                ) {
                                    Text(if (isAssembly) "🎙️" else "⚡", fontSize = 18.sp)
                                    Spacer(modifier = Modifier.width(10.dp))
                                    Column {
                                        Text(
                                            text = if (isAssembly) "Active Transcription Engine" else "Active Model",
                                            color = ext.textMuted,
                                            fontSize = 10.sp,
                                            fontWeight = FontWeight.SemiBold
                                        )
                                        Text(
                                            text = currentSTT,
                                            color = if (isAssembly) Color(0xFFCE93D8) else Color(0xFFD4AF37),
                                            fontSize = 14.sp,
                                            fontWeight = FontWeight.Bold
                                        )
                                        if (isAssembly) {
                                            Text(
                                                text = "Intelligence: $currentIntel",
                                                color = ext.textMuted,
                                                fontSize = 11.sp,
                                                fontWeight = FontWeight.Medium
                                            )
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
                }
                SettingsSection("🎧", "Upload audio (test)", "Try a model on a file", openSection == "upload", { toggle("upload") }) {
                Surface(
                    color = colors.surface,
                    shape = RoundedCornerShape(14.dp),
                    border = BorderStroke(1.dp, CyanAccent.copy(alpha = 0.5f)),
                    modifier = Modifier.fillMaxWidth()
                ) {
                    Column(modifier = Modifier.padding(16.dp)) {
                        Row(verticalAlignment = Alignment.CenterVertically) {
                            Text("📤", fontSize = 16.sp)
                            Spacer(modifier = Modifier.width(6.dp))
                            Column(modifier = Modifier.weight(1f)) {
                                Text("UPLOAD AUDIO (TEST A MODEL)", color = CyanAccent, fontSize = 11.sp, fontWeight = FontWeight.Bold)
                                Text(
                                    text = "Transcribe any audio file with the model you pick. It appears in Recordings with its transcript, summary and cost.",
                                    color = colors.onSurfaceVariant,
                                    fontSize = 12.sp
                                )
                            }
                        }

                        Spacer(modifier = Modifier.height(12.dp))

                        OutlinedButton(
                            onClick = { pickAudioLauncher.launch(arrayOf("audio/*", "video/mp4")) },
                            enabled = importProgress == null,
                            border = BorderStroke(1.dp, colors.outline),
                            shape = RoundedCornerShape(10.dp),
                            modifier = Modifier.fillMaxWidth()
                        ) {
                            Text(
                                text = if (importUri == null) "🎵 Choose audio file" else "🎵 $importFileName" +
                                    if (importFileSize > 0) " · ${String.format("%.1f", importFileSize / 1048576.0)} MB" else "",
                                color = colors.onBackground,
                                fontSize = 13.sp,
                                maxLines = 1,
                                overflow = androidx.compose.ui.text.style.TextOverflow.Ellipsis
                            )
                        }

                        Spacer(modifier = Modifier.height(8.dp))
                        OutlinedTextField(
                            value = importTitle,
                            onValueChange = { importTitle = it },
                            label = { Text("Title (optional)") },
                            singleLine = true,
                            enabled = importProgress == null,
                            modifier = Modifier.fillMaxWidth()
                        )

                        Spacer(modifier = Modifier.height(10.dp))
                        Text("Model", color = colors.onSurfaceVariant, fontSize = 12.sp, fontWeight = FontWeight.SemiBold)
                        Spacer(modifier = Modifier.height(4.dp))
                        Column(verticalArrangement = Arrangement.spacedBy(6.dp)) {
                            availableModels.filter {
                                (it["supports"] as? List<*>)?.contains("transcription") != false
                            }.forEach { model ->
                                val modelId = model["id"]?.toString() ?: ""
                                val isSelected = importModel == modelId
                                ModelOptionRow(model, isSelected, accent = CyanAccent, enabled = importProgress == null,
                                    onSelect = { importModel = modelId })
                            }
                        }

                        Spacer(modifier = Modifier.height(12.dp))
                        val progress = importProgress
                        if (progress != null) {
                            LinearProgressIndicator(
                                progress = { progress },
                                color = CyanAccent,
                                modifier = Modifier.fillMaxWidth()
                            )
                            Spacer(modifier = Modifier.height(4.dp))
                            Text(
                                text = "Uploading ${(progress * 100).toInt()}% — keep this screen open",
                                color = colors.onSurfaceVariant,
                                fontSize = 11.sp
                            )
                        } else {
                            Button(
                                onClick = {
                                    val uri = importUri ?: return@Button
                                    importProgress = 0f
                                    importMessage = null
                                    scope.launch {
                                        val res = apiClient.importAudio(
                                            uri = uri,
                                            fileName = importFileName,
                                            sizeBytes = importFileSize,
                                            model = importModel,
                                            title = importTitle.ifBlank { null },
                                            onProgress = { p -> importProgress = p }
                                        )
                                        importProgress = null
                                        if (res.isSuccess) {
                                            val modelName = availableModels.firstOrNull { it["id"] == importModel }?.get("name") ?: importModel
                                            importMessage = "✅ Uploaded. Transcribing with $modelName — open Recordings to see \"${res.getOrNull()?.get("title") ?: importTitle}\"."
                                            importUri = null
                                            importFileName = ""
                                            importFileSize = 0L
                                            importTitle = ""
                                        } else {
                                            importMessage = "❌ ${res.exceptionOrNull()?.message ?: "Upload failed"}"
                                        }
                                    }
                                },
                                enabled = importUri != null,
                                colors = ButtonDefaults.buttonColors(containerColor = CyanAccent),
                                shape = RoundedCornerShape(10.dp),
                                modifier = Modifier.fillMaxWidth()
                            ) {
                                Text("Upload & transcribe", color = ext.bgDark, fontWeight = FontWeight.Bold)
                            }
                        }

                        if (importMessage != null) {
                            Spacer(modifier = Modifier.height(6.dp))
                            Text(
                                text = importMessage!!,
                                color = if (importMessage!!.startsWith("✅")) EmeraldPrimary else Color(0xFFFF5252),
                                fontSize = 12.sp,
                                fontWeight = FontWeight.Medium
                            )
                        }
                    }
                }
                }
                SettingsSection("⏸", "Unfinished sessions", unfinishedSummary, openSection == "unfinished", { toggle("unfinished") }) {
                    UnfinishedSessionsSection(apiClient)
                }
            }

            SettingsSection("ℹ️", "About", "v${com.isf.recorder.BuildConfig.VERSION_NAME}", openSection == "about", { toggle("about") }) {
                AboutCard(syncTriggered = syncTriggered, onSync = {
                    try {
                        // Core-owned scheduling (unique work, KEEP) — don't cancel a running upload.
                        com.isf.recorder.sync.ChunkUploader.scheduleOfflineSync(context)
                        syncTriggered = true
                    } catch (e: Exception) {
                        Toast.makeText(context, "Could not schedule sync", Toast.LENGTH_SHORT).show()
                    }
                })
            }

            Spacer(modifier = Modifier.height(24.dp))
        }
    }

    // ── Add / Edit Prompt Template Dialog (Admin Only) ─────────────────────────
    if (showTemplateDialog) {
        val isEditing = editingTemplateId != null
        AlertDialog(
            onDismissRequest = { if (!isSavingTemplate) showTemplateDialog = false },
            containerColor = colors.surface,
            title = {
                Text(
                    text = if (isEditing) "Edit Prompt Template" else "New Prompt Template",
                    color = colors.onBackground,
                    fontSize = 18.sp,
                    fontWeight = FontWeight.Bold
                )
            },
            text = {
                Column(
                    modifier = Modifier
                        .fillMaxWidth()
                        .verticalScroll(rememberScrollState())
                ) {
                    Text("Title (shown in / command):", color = colors.onSurfaceVariant, fontSize = 12.sp, fontWeight = FontWeight.SemiBold)
                    Spacer(modifier = Modifier.height(4.dp))
                    OutlinedTextField(
                        value = tmplTitle,
                        onValueChange = { tmplTitle = it },
                        placeholder = { Text("e.g. Daily Report, Hinglish Summary", color = ext.textMuted) },
                        singleLine = true,
                        colors = OutlinedTextFieldDefaults.colors(
                            focusedTextColor = colors.onBackground,
                            unfocusedTextColor = colors.onBackground,
                            focusedBorderColor = CyanAccent,
                            unfocusedBorderColor = colors.outline,
                            cursorColor = CyanAccent
                        ),
                        modifier = Modifier.fillMaxWidth()
                    )

                    Spacer(modifier = Modifier.height(12.dp))

                    Text("Icon (Emoji):", color = colors.onSurfaceVariant, fontSize = 12.sp, fontWeight = FontWeight.SemiBold)
                    Spacer(modifier = Modifier.height(4.dp))
                    val availableIcons = listOf("📋", "📝", "✅", "💬", "🗣️", "📊", "🎯", "📑", "⚡", "🔍")
                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        horizontalArrangement = Arrangement.spacedBy(6.dp)
                    ) {
                        availableIcons.forEach { ic ->
                            Surface(
                                shape = RoundedCornerShape(8.dp),
                                color = if (tmplIcon == ic) CyanAccent.copy(alpha = 0.3f) else colors.surfaceVariant,
                                border = BorderStroke(1.dp, if (tmplIcon == ic) CyanAccent else colors.outline),
                                modifier = Modifier
                                    .size(34.dp)
                                    .clickable { tmplIcon = ic }
                            ) {
                                Box(contentAlignment = Alignment.Center) {
                                    Text(ic, fontSize = 16.sp)
                                }
                            }
                        }
                    }

                    Spacer(modifier = Modifier.height(12.dp))

                    Text("Prompt Instruction for AI:", color = colors.onSurfaceVariant, fontSize = 12.sp, fontWeight = FontWeight.SemiBold)
                    Spacer(modifier = Modifier.height(4.dp))
                    OutlinedTextField(
                        value = tmplPrompt,
                        onValueChange = { tmplPrompt = it },
                        placeholder = { Text("Instructions describing how Gemini should format the response...", color = ext.textMuted) },
                        minLines = 3,
                        maxLines = 6,
                        colors = OutlinedTextFieldDefaults.colors(
                            focusedTextColor = colors.onBackground,
                            unfocusedTextColor = colors.onBackground,
                            focusedBorderColor = CyanAccent,
                            unfocusedBorderColor = colors.outline,
                            cursorColor = CyanAccent
                        ),
                        modifier = Modifier.fillMaxWidth()
                    )

                    Spacer(modifier = Modifier.height(12.dp))

                    Text("Example Output Format (Optional):", color = colors.onSurfaceVariant, fontSize = 12.sp, fontWeight = FontWeight.SemiBold)
                    Spacer(modifier = Modifier.height(4.dp))
                    OutlinedTextField(
                        value = tmplExample,
                        onValueChange = { tmplExample = it },
                        placeholder = { Text("Show Gemini an example of headings, bullets, and layout...", color = ext.textMuted) },
                        minLines = 2,
                        maxLines = 5,
                        colors = OutlinedTextFieldDefaults.colors(
                            focusedTextColor = colors.onBackground,
                            unfocusedTextColor = colors.onBackground,
                            focusedBorderColor = CyanAccent,
                            unfocusedBorderColor = colors.outline,
                            cursorColor = CyanAccent
                        ),
                        modifier = Modifier.fillMaxWidth()
                    )

                    if (templateError != null) {
                        Spacer(modifier = Modifier.height(8.dp))
                        Text(templateError!!, color = Color(0xFFFF5252), fontSize = 12.sp)
                    }
                }
            },
            confirmButton = {
                Button(
                    onClick = {
                        val titleTrim = tmplTitle.trim()
                        val promptTrim = tmplPrompt.trim()
                        if (titleTrim.isEmpty()) {
                            templateError = "Title cannot be empty"
                            return@Button
                        }
                        if (promptTrim.isEmpty()) {
                            templateError = "Prompt instruction cannot be empty"
                            return@Button
                        }

                        isSavingTemplate = true
                        templateError = null

                        scope.launch {
                            val res = if (isEditing) {
                                apiClient.updateChatTemplate(
                                    id = editingTemplateId!!,
                                    title = titleTrim,
                                    prompt = promptTrim,
                                    exampleOutput = tmplExample.trim().ifEmpty { null },
                                    icon = tmplIcon
                                )
                            } else {
                                apiClient.createChatTemplate(
                                    title = titleTrim,
                                    prompt = promptTrim,
                                    exampleOutput = tmplExample.trim().ifEmpty { null },
                                    icon = tmplIcon,
                                    sortOrder = 0
                                )
                            }

                            isSavingTemplate = false
                            if (res.isSuccess) {
                                showTemplateDialog = false
                                Toast.makeText(context, if (isEditing) "Template updated!" else "Template created!", Toast.LENGTH_SHORT).show()
                                loadChatTemplates()
                            } else {
                                templateError = res.exceptionOrNull()?.message ?: "Failed to save template"
                            }
                        }
                    },
                    colors = ButtonDefaults.buttonColors(containerColor = CyanAccent),
                    enabled = !isSavingTemplate
                ) {
                    if (isSavingTemplate) {
                        CircularProgressIndicator(modifier = Modifier.size(16.dp), color = ext.bgDark, strokeWidth = 2.dp)
                    } else {
                        Text(if (isEditing) "Update Template" else "Save Template", color = ext.bgDark, fontWeight = FontWeight.Bold)
                    }
                }
            },
            dismissButton = {
                TextButton(
                    onClick = { showTemplateDialog = false },
                    enabled = !isSavingTemplate
                ) {
                    Text("Cancel", color = ext.textMuted)
                }
            }
        )
    }
}
