package com.isf.recorder.ui.screens

import android.Manifest
import android.content.Intent
import android.widget.Toast
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.animation.core.*
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.ScrollState
import androidx.compose.foundation.background
import androidx.compose.material.icons.filled.KeyboardArrowUp
import androidx.compose.material.icons.filled.KeyboardArrowDown
import androidx.compose.material.icons.Icons
import androidx.compose.foundation.clickable
import androidx.compose.animation.AnimatedVisibility
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.scale
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.layout.onSizeChanged
import androidx.compose.ui.layout.onGloballyPositioned
import androidx.compose.ui.layout.layout
import androidx.compose.ui.layout.LayoutCoordinates
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.core.content.ContextCompat
import androidx.lifecycle.viewmodel.compose.viewModel
import com.google.gson.Gson
import com.isf.recorder.R
import com.isf.recorder.data.api.ISFApiClient
import com.isf.recorder.data.db.AppDatabase
import com.isf.recorder.data.model.FinishedSession
import com.isf.recorder.data.model.RecordingSessionInfo
import com.isf.recorder.data.model.RecordingStatus
import com.isf.recorder.data.prefs.UserPreferences
import com.isf.recorder.service.AudioRecordingService
import com.isf.recorder.service.MicReadiness
import com.isf.recorder.service.MicStatus
import com.isf.recorder.sync.ChunkUploader
import com.isf.recorder.sync.NetworkMonitor
import com.isf.recorder.ui.components.*
import com.isf.recorder.ui.theme.*
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.withTimeoutOrNull
import kotlinx.coroutines.withContext


/**
 * Jomi v2 Home — ONE page for coaches (round 2): record, the player for the selected
 * recording (default = last finished), its status, the report as a popup, and the
 * recordings list (See all expands in place for coaches; admins get the All-recordings page).
 * Long-press a recording to select several → Merge / Delete.
 */
@Composable
fun HomeScreen(
    onSeeAll: () -> Unit,                         // admin: All-recordings page
    onOpenChat: (recordingId: String, title: String) -> Unit,
    onSettings: () -> Unit,
    onOpenFullReport: (String) -> Unit,           // admin only: Report screen (transcript & versions)
    onOpenTranscript: (String) -> Unit = {},      // everyone: Transcript screen (copy / share)
    vm: HomeViewModel = viewModel()
) {
    val context = LocalContext.current
    val prefs = remember { UserPreferences(context) }
    val api = remember { ISFApiClient(context) }
    val scope = rememberCoroutineScope()
    val scrollState = rememberScrollState()
    val isAdmin = prefs.isAdmin

    val session by AudioRecordingService.sessionState.collectAsState()
    val finished by AudioRecordingService.lastFinished.collectAsState()
    val liveAmplitude by AudioRecordingService.amplitude.collectAsState()
    val hasSession = session != null || vm.starting

    LaunchedEffect(Unit) { vm.refresh() }
    LaunchedEffect(session != null) { vm.onSessionActive(session != null) }
    LaunchedEffect(vm.starting) {
        if (vm.starting) {
            delay(8_000)
            if (AudioRecordingService.sessionState.value == null) vm.starting = false
        }
    }
    // A newly finished recording becomes the player's default again.
    LaunchedEffect(finished?.recordingId) { vm.onFinished(finished?.recordingId) }

    val finishedRec: Map<String, Any?>? = finished
        ?.takeIf { !it.uploadPending && !it.recordingId.isNullOrBlank() }
        ?.let { mapOf("id" to it.recordingId, "title" to it.title, "voice_seconds" to it.voiceSeconds) }
    val target = vm.selected ?: finishedRec
    val targetId = target?.get("id")?.toString()
    LaunchedEffect(targetId) { vm.watch(targetId) }

    // Real mic check only while idle (it opens an AudioRecord briefly).
    var mic by remember { mutableStateOf(MicReadiness.READY) }
    val externalMic = rememberExternalMic()                      // instruction 1: USB-C / wired mic present?
    var confirmPhoneMic by remember { mutableStateOf(false) }    // instruction 2: ask before using the phone mic
    LaunchedEffect(hasSession) {
        while (!hasSession) {
            mic = withContext(Dispatchers.IO) { MicStatus.check(context) }
            delay(10_000)
        }
    }
    val permissionLauncher = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
        scope.launch { mic = withContext(Dispatchers.IO) { MicStatus.check(context) } }
        if (!granted) Toast.makeText(context, "Microphone permission is needed to record", Toast.LENGTH_LONG).show()
    }

    // the open report / Director's POV popup comes back after switching apps (only id + title are saved)
    var reportFor by rememberSaveable(stateSaver = OpenRecordingSaver) { mutableStateOf<Map<String, Any?>?>(null) }
    var povFor by rememberSaveable(stateSaver = OpenRecordingSaver) { mutableStateOf<Map<String, Any?>?>(null) }
    var expandedId by rememberSaveable { mutableStateOf<String?>(null) }   // the one open recording card (kept on Back)
    // Tapping a card jumps up to the player; closing a report / Director's POV opened from there scrolls back down
    // to that card (21 Oct). Only while the person is still up at the player (they did not scroll away themselves).
    var returnScroll by rememberSaveable { mutableStateOf<Int?>(null) }
    val scrollContent = remember { CoordsHolder() }     // the scrolling content (to find the player inside it)
    val playerAnchor = remember { CoordsHolder() }      // the player block
    fun playerY(): Int? {
        val content = scrollContent.coords ?: return null
        val player = playerAnchor.coords?.takeIf { it.isAttached } ?: return null
        return content.localPositionOf(player, androidx.compose.ui.geometry.Offset.Zero).y.toInt().coerceAtLeast(0)
    }
    var scrollToPlayer by remember { mutableStateOf(false) }
    LaunchedEffect(scrollToPlayer) {
        if (!scrollToPlayer) return@LaunchedEffect
        delay(60)                                          // let the newly selected player lay out
        scrollState.animateScrollTo(((playerY() ?: 0) - 12).coerceAtLeast(0))
        scrollToPlayer = false
    }
    var returnOnEnter by rememberSaveable { mutableStateOf<Int?>(null) }   // full report / transcript opened: on Back
    fun backToCard(leaving: Boolean = false) {   // leaving = another screen opens right away (Home is rebuilt on Back)
        val to = returnScroll ?: return
        returnScroll = null
        // only while the person is still at the player (they did not scroll away themselves)
        if (kotlin.math.abs(scrollState.value - (playerY() ?: 0)) >= 300) return
        if (leaving) returnOnEnter = to else scope.launch { scrollState.animateScrollTo(to) }
    }
    LaunchedEffect(Unit) {
        val to = returnOnEnter ?: return@LaunchedEffect
        returnOnEnter = null
        // wait (max ~1 s) until the list is laid out far enough to reach the card
        withTimeoutOrNull(1_000) { snapshotFlow { scrollState.maxValue }.first { it >= to } }
        scrollState.scrollTo(to)
    }
    var callsRefresh by remember { mutableStateOf(0) }   // reload Calls after a call popup closes
    var editTarget by remember { mutableStateOf<Map<String, Any?>?>(null) }
    var eraseTargets by remember { mutableStateOf<List<Map<String, Any?>>?>(null) }
    var mergeTargets by remember { mutableStateOf<List<Map<String, Any?>>?>(null) }
    var selectedIds by rememberSaveable(stateSaver = SelectedIdsSaver) { mutableStateOf(setOf<String>()) }   // kept on Back
    val selectionMode = selectedIds.isNotEmpty()
    // Phone Back on Home: first removes the long-press ticks, then closes "See all";
    // with nothing open, MainActivity's "Press Back again to exit" takes over.
    // ...and after tapping a card (which jumps up to the player) Back first returns to that card.
    val canReturnToCard by remember {
        derivedStateOf { returnScroll != null && kotlin.math.abs(scrollState.value - (playerY() ?: 0)) < 300 }
    }
    androidx.activity.compose.BackHandler(enabled = selectionMode || canReturnToCard || vm.allExpanded) {
        when {
            selectionMode -> selectedIds = emptySet()
            canReturnToCard -> backToCard()
            else -> vm.allExpanded = false
        }
    }

    // Notification deep link (coaches): load the recording and open its report popup.
    LaunchedEffect(vm.pendingReportId) {
        val id = vm.pendingReportId ?: return@LaunchedEffect
        vm.pendingReportId = null
        val rec = api.getRecordingDetail(id).getOrNull() ?: mapOf("id" to id)
        vm.select(rec + ("id" to id))
        reportFor = rec + ("id" to id)
    }

    val cfg = vm.config
    val mode = session?.mode?.takeIf { it == "schedule" || it == "meeting" } ?: vm.effectiveMode
    LaunchedEffect(mode, cfg.loaded) { if (cfg.loaded) vm.setListModeTo(mode) }
    val ready = when (mode) {
        "schedule" -> vm.balgruh != null && vm.coaches.isNotEmpty()
        else -> vm.participants.isNotEmpty()
    }

    fun peopleJson(list: List<NameOption>) = Gson().toJson(list.map { p ->
        if (p.userId != null) mapOf("user_id" to p.userId, "name" to p.name) else mapOf("name" to p.name)
    })

    fun doStartRecording() {
        val intent = Intent(context, AudioRecordingService::class.java).apply {
            action = AudioRecordingService.ACTION_START
            putExtra(AudioRecordingService.EXTRA_MODE, mode)
            putExtra(AudioRecordingService.EXTRA_SILENCE_STOP_MIN, cfg.silenceStopMinutes[mode] ?: 0)
            if (mode == "schedule") {
                putExtra(AudioRecordingService.EXTRA_BALGRUH, vm.balgruh?.name)
                putExtra(AudioRecordingService.EXTRA_ASSISTANT_COACH, vm.coaches.joinToString(", ") { it.name })
                putExtra(AudioRecordingService.EXTRA_ASSISTANT_COACHES_JSON, peopleJson(vm.coaches))
            } else {
                vm.meetingName.trim().takeIf { it.isNotEmpty() }?.let { putExtra(AudioRecordingService.EXTRA_TITLE, it) }
                putExtra(AudioRecordingService.EXTRA_PARTICIPANTS_JSON, peopleJson(vm.participants))
            }
        }
        vm.starting = true
        try {
            ContextCompat.startForegroundService(context, intent)
        } catch (e: Exception) {
            vm.starting = false
            Toast.makeText(context, "Could not start recording: ${e.message}", Toast.LENGTH_LONG).show()
        }
    }

    fun startRecording() {
        if (mic == MicReadiness.NO_PERMISSION) {
            permissionLauncher.launch(Manifest.permission.RECORD_AUDIO)
            return
        }
        if (externalMic == null) confirmPhoneMic = true else doStartRecording()
    }

    fun sendAction(action: String) {
        try {
            context.startService(Intent(context, AudioRecordingService::class.java).apply { this.action = action })
        } catch (e: Exception) {
            Toast.makeText(context, "Recorder not reachable: ${e.message}", Toast.LENGTH_SHORT).show()
        }
    }

    fun toggleSelect(id: String) {
        selectedIds = if (id in selectedIds) selectedIds - id else selectedIds + id
    }

    @Composable
    fun Card(rec: Map<String, Any?>) {
        val id = rec["id"]?.toString().orEmpty()
        RecordingCard(
            rec = rec,
            highlighted = id == targetId,
            showOwner = isAdmin,                 // admins see every coach's recordings: show who recorded it
            onOpen = {
                vm.select(rec)
                returnScroll = scrollState.value          // where the card was: closing its report comes back here
                scrollToPlayer = true                     // jump up to the player (after it is laid out)
            },
            onReport = { reportFor = rec },
            onChat = { onOpenChat(id, cleanTitle(rec)) },
            onCopy = { scope.launch { copyOrShareRecording(context, api, rec, share = false) } },
            onShare = { scope.launch { copyOrShareRecording(context, api, rec, share = true) } },
            onEditTitle = { editTarget = rec },
            onDelete = { eraseTargets = listOf(rec) },
            selectionMode = selectionMode,
            selected = id in selectedIds,
            onToggleSelect = { toggleSelect(id) },
            onLongPress = { toggleSelect(id) },
            // Director's POV: admins only (not while a super admin views as Coach), never calls
            onDirectorPov = if (isAdmin && rec["source"]?.toString() != "call") ({ povFor = rec }) else null,
            // Collapsed by default; one card open at a time
            expanded = expandedId == id,
            onToggleExpand = { expandedId = if (expandedId == id) null else id }
        )
    }

    // Home redesign (21 Oct): teal header, Meeting / Schedule / Calls card, ISF Bandhu card, the player, Previous
    // recordings; the recorder is a panel fixed at the bottom (always reachable without scrolling).
    var panelHeightPx by remember { mutableIntStateOf(0) }
    val density = LocalDensity.current
    val panelHeight = with(density) { panelHeightPx.toDp() }
    val showPanel = !(vm.callsTab && !hasSession)
    var bandhuSoon by remember { mutableStateOf(false) }
    var panelExpanded by rememberSaveable { mutableStateOf(false) }   // recorder panel: only the buttons until opened

    Box(Modifier.fillMaxSize().background(MaterialTheme.colorScheme.background)) {
        Column(
            modifier = Modifier
                .fillMaxSize()
                .verticalScroll(scrollState)
                .onGloballyPositioned { scrollContent.coords = it }
        ) {
            HomeHeader(prefs, cfg, onSettings)
            Column(Modifier.padding(horizontal = 16.dp)) {
                if (cfg.modes.size > 1) {
                    HomeTabs(cfg.modes + "calls", if (vm.callsTab && !hasSession) "calls" else mode,
                        { if (it == "calls") vm.callsTab = true else { vm.callsTab = false; vm.mode = it } },
                        enabled = !hasSession, modifier = Modifier.pullUp(34.dp))
                } else Spacer(Modifier.height(16.dp))
                Spacer(Modifier.height(12.dp))
                NotificationsOffBanner(Modifier.padding(bottom = 12.dp))

                if (vm.callsTab && !hasSession) {
                    CallsTab(api, isAdmin, callsRefresh, onOpenCall = { reportFor = it }, onOpenSettings = onSettings)
                } else {
                    BandhuCard(onAsk = { bandhuSoon = true })

                    // ── Player + status for the selected / last recording ─────────────────
                    if (!hasSession) {
                        if (finished?.uploadPending == true && vm.selected == null) {
                            Spacer(Modifier.height(12.dp))
                            TranscriptionStatusCard(TranscriptionUi.Offline, {}, {})
                        } else if (target != null && targetId != null) {
                            Spacer(Modifier.height(12.dp))
                            Column(Modifier.onGloballyPositioned { playerAnchor.coords = it }) {
                                PlayerCard(
                                    rec = target,
                                    statusItem = vm.statusItem,
                                    api = api,
                                    retrying = vm.retrying,
                                    onRetry = { vm.retry(cleanTitle(target)) { Toast.makeText(context, it, Toast.LENGTH_LONG).show() } },
                                    onReport = { reportFor = (vm.statusItem?.let { target + it } ?: target) },
                                    onChat = { onOpenChat(targetId, cleanTitle(target)) },
                                    onCopy = { scope.launch { copyOrShareRecording(context, api, target, share = false) } },
                                    onShare = { scope.launch { copyOrShareRecording(context, api, target, share = true) } },
                                    showOwner = isAdmin,
                                    // Director's POV: admins only (not while a super admin views as Coach), never calls
                                    onDirectorPov = if (isAdmin && target["source"]?.toString() != "call") ({ povFor = target + ("id" to targetId) }) else null
                                )
                            }
                        }
                    }

                // ── Recordings ─────────────────────────────────────────────────────────
                // "See all" stays open when coming Back from a report / chat / transcript; Settings resets it (MainActivity)
                Spacer(Modifier.height(20.dp))
                Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.fillMaxWidth()) {
                    Text(
                        if (vm.allExpanded) (if (isAdmin) "All recordings" else "All my recordings") else "Previous recordings",
                        color = MaterialTheme.colorScheme.onBackground,
                        fontSize = 17.sp,
                        fontWeight = FontWeight.Bold,
                        modifier = Modifier.weight(1f)
                    )
                    TextButton(onClick = {
                        // See all opens in place for everyone (admins too: no separate page, 13 Oct)
                        vm.allExpanded = !vm.allExpanded
                        if (vm.allExpanded) vm.loadAll(reset = true)
                    }) {
                        Text(if (vm.allExpanded) "Show less" else "See all", color = JomiTeal, fontWeight = FontWeight.SemiBold)
                    }
                }
                RecordingFilters(vm = vm, cfg = cfg, isAdmin = isAdmin, schedule = mode == "schedule")
                Spacer(Modifier.height(6.dp))
                if (!selectionMode && (vm.recent.isNotEmpty() || vm.allExpanded)) {
                    Text("Tip: tap a recording to play it here; long-press to select several.", color = JomiTextMuted, fontSize = 12.sp)
                }
                Spacer(Modifier.height(6.dp))

                if (vm.allExpanded) {
                    if (vm.allItems.isEmpty() && !vm.allLoading) {
                        Text(if (vm.filtersActive) "No recordings match these filters." else "No recordings yet.",
                            color = JomiTextMuted, fontSize = 13.sp, modifier = Modifier.padding(vertical = 12.dp))
                    }
                    Column(verticalArrangement = Arrangement.spacedBy(12.dp)) {
                        vm.allItems.forEach { rec -> key(rec["id"]) { Card(rec) } }
                    }
                    if (vm.allLoading) {
                        Box(Modifier.fillMaxWidth().padding(16.dp), contentAlignment = Alignment.Center) {
                            CircularProgressIndicator(color = JomiGreen, modifier = Modifier.size(24.dp), strokeWidth = 2.dp)
                        }
                    } else if (vm.allItems.size < vm.allTotal) {
                        OutlinedButton(
                            onClick = { vm.loadAll(reset = false) },
                            border = BorderStroke(1.dp, JomiMintBorder),
                            modifier = Modifier.fillMaxWidth().padding(top = 12.dp)
                        ) { Text("Load older (${vm.allItems.size} of ${vm.allTotal})", color = JomiTeal) }
                    }
                    TextButton(
                        onClick = { vm.allExpanded = false },
                        modifier = Modifier.fillMaxWidth().padding(top = 4.dp)
                    ) { Text("Show less", color = JomiTeal, fontWeight = FontWeight.SemiBold) }
                } else when {
                    vm.recentLoading -> Box(Modifier.fillMaxWidth().padding(24.dp), contentAlignment = Alignment.Center) {
                        CircularProgressIndicator(color = JomiGreen, modifier = Modifier.size(24.dp), strokeWidth = 2.dp)
                    }
                    vm.recent.isEmpty() -> Text(
                        text = vm.recentError?.let { "Could not load recordings: $it" }
                            ?: if (vm.filtersActive) "No recordings match these filters." else "No recordings yet. Your recordings will appear here.",
                        color = JomiTextMuted,
                        fontSize = 13.sp,
                        modifier = Modifier.padding(vertical = 12.dp)
                    )
                    else -> Column(verticalArrangement = Arrangement.spacedBy(12.dp)) {
                        vm.recent.forEach { rec -> key(rec["id"]) { Card(rec) } }
                        OutlinedButton(
                            onClick = { vm.allExpanded = true; vm.loadAll(reset = true) },
                            border = BorderStroke(1.dp, JomiMintBorder),
                            modifier = Modifier.fillMaxWidth()
                        ) { Text("See all recordings", color = JomiTeal, fontWeight = FontWeight.SemiBold) }
                    }
                }

                }   // end of recordings view (Calls tab replaces it)
                Spacer(Modifier.height((if (showPanel) panelHeight else 0.dp) + if (selectionMode) 96.dp else 24.dp))
            }
        }

        if (showPanel) {
            RecorderPanel(
                mode = mode,
                cfg = cfg,
                vm = vm,
                session = session,
                starting = vm.starting,
                finished = finished?.takeIf { vm.selected == null },
                mic = mic,
                externalMic = externalMic,
                amplitude = liveAmplitude,
                canStart = !hasSession && mic != MicReadiness.IN_CALL && ready,
                locked = hasSession,
                onRecord = { startRecording() },
                onPause = { sendAction(AudioRecordingService.ACTION_PAUSE) },
                onResume = { sendAction(AudioRecordingService.ACTION_RESUME) },
                onStop = { sendAction(AudioRecordingService.ACTION_STOP) },
                onUsePhoneMic = { sendAction(AudioRecordingService.ACTION_USE_PHONE_MIC) },
                expanded = panelExpanded,
                onToggle = { panelExpanded = !panelExpanded },
                modifier = Modifier.align(Alignment.BottomCenter).onSizeChanged { panelHeightPx = it.height }
            )
        }

        ScrollToTopButton(
            scrollState,
            Modifier.align(Alignment.BottomEnd).padding(end = 16.dp,
                bottom = (if (showPanel) panelHeight else 0.dp) + if (selectionMode) 84.dp else 12.dp)
        )

        if (selectionMode) {
            val pool = (vm.recent + vm.allItems).distinctBy { it["id"] }
            SelectionActionBar(
                count = selectedIds.size,
                onMerge = { mergeTargets = pool.filter { it["id"]?.toString() in selectedIds } },
                onDelete = { eraseTargets = pool.filter { it["id"]?.toString() in selectedIds } },
                onCancel = { selectedIds = emptySet() },
                modifier = Modifier.align(Alignment.BottomCenter).padding(start = 12.dp, end = 12.dp,
                    bottom = (if (showPanel) panelHeight else 0.dp) + 12.dp)
            )
        }
    }

    if (bandhuSoon) {
        AlertDialog(
            onDismissRequest = { bandhuSoon = false },
            title = { Text("🤖 ISF Bandhu", fontWeight = FontWeight.Bold) },
            text = { Text("Coming soon! You will be able to ask ISF Bandhu specific questions here.") },
            confirmButton = { TextButton(onClick = { bandhuSoon = false }) { Text("OK", color = JomiTeal) } }
        )
    }

    if (confirmPhoneMic) {
        PhoneMicConfirmDialog(
            onConfirm = { confirmPhoneMic = false; doStartRecording() },
            onDismiss = { confirmPhoneMic = false }
        )
    }

    reportFor?.let { rec ->
        val id = rec["id"]?.toString().orEmpty()
        ReportSheet(
            recordingId = id,
            title = cleanTitle(rec),
            onDismiss = { reportFor = null; callsRefresh++; backToCard() },
            onOpenFull = if (isAdmin) ({ reportFor = null; backToCard(leaving = true); onOpenFullReport(id) }) else null,
            onOpenTranscript = { reportFor = null; backToCard(leaving = true); onOpenTranscript(id) }
        )
    }
    povFor?.let { rec ->
        if (isAdmin) DirectorPovSheet(recordingId = rec["id"]?.toString().orEmpty(), title = cleanTitle(rec), onDismiss = { povFor = null; backToCard() })
    }
    editTarget?.let { rec ->
        EditTitleDialog(
            initial = cleanTitle(rec),
            onDismiss = { editTarget = null },
            onSave = { newTitle ->
                val res = api.updateRecordingTitle(rec["id"].toString(), newTitle)
                if (res.isSuccess) { vm.reloadLists(); null } else res.exceptionOrNull()?.message ?: "Could not rename"
            }
        )
    }
    eraseTargets?.let { recs ->
        BulkEraseDialog(api, recs, onDismiss = { eraseTargets = null }, onDone = {
            eraseTargets = null
            if (recs.any { it["id"]?.toString() == targetId }) vm.select(null)
            selectedIds = emptySet()
            vm.reloadLists()
        })
    }
    mergeTargets?.let { recs ->
        MergeDialog(
            api, recs,
            onDismiss = { mergeTargets = null },
            onStarted = { mergeTargets = null; selectedIds = emptySet() },
            onMerged = { vm.reloadLists() }
        )
    }
}

/** Holds layout coordinates without causing recomposition. */
private class CoordsHolder { var coords: LayoutCoordinates? = null }

/** Moves an element up by [by] and takes that much less space (the tabs card overlapping the header). */
private fun Modifier.pullUp(by: androidx.compose.ui.unit.Dp) = layout { measurable, constraints ->
    val placeable = measurable.measure(constraints)
    val px = by.roundToPx()
    layout(placeable.width, (placeable.height - px).coerceAtLeast(0)) { placeable.place(0, -px) }
}

/** Teal header (21 Oct): avatar (opens Settings), name, "ISF · ADMIN" pill, date, Live sync pill. */
@Composable
private fun HomeHeader(prefs: UserPreferences, cfg: RecorderConfig, onSettings: () -> Unit) {
    val context = LocalContext.current
    val networkMonitor = remember { NetworkMonitor.getInstance(context) }
    val chunkDao = remember { AppDatabase.getInstance(context).chunkDao() }
    val isOnline by networkMonitor.isOnline.collectAsState()
    val pending by chunkDao.getPendingCountFlow().collectAsState(initial = 0)
    val name = prefs.userName?.takeIf { it.isNotBlank() } ?: cfg.roleLabel
    val roleText = if (prefs.isAdmin) "Admin" else cfg.roleLabel

    Box(
        Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(bottomStart = 32.dp, bottomEnd = 32.dp))
            .background(Brush.verticalGradient(listOf(Color(0xFF16A37F), Color(0xFF0F8F72))))
            .padding(start = 16.dp, end = 16.dp, top = 12.dp, bottom = 46.dp)
    ) {
        Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.fillMaxWidth()) {
            // Avatar opens Settings directly (round 2)
            Surface(onClick = onSettings, shape = CircleShape, color = Color.White, modifier = Modifier.size(48.dp)) {
                Box(contentAlignment = Alignment.Center) {
                    Text(initialsOf(name), color = JomiTeal, fontWeight = FontWeight.Bold, fontSize = 17.sp)
                }
            }
            Spacer(Modifier.width(12.dp))
            Column(Modifier.weight(1f)) {
                Text(name, color = Color.White, fontSize = 17.sp, fontWeight = FontWeight.Bold, maxLines = 1, overflow = TextOverflow.Ellipsis)
                Spacer(Modifier.height(3.dp))
                Surface(color = Color.White.copy(alpha = 0.2f), shape = RoundedCornerShape(10.dp)) {
                    Text("${prefs.orgName ?: "ISF"} · ${roleText.uppercase()}", color = Color.White, fontSize = 9.sp,
                        fontWeight = FontWeight.Bold, letterSpacing = 0.8.sp, maxLines = 1,
                        modifier = Modifier.padding(horizontal = 6.dp, vertical = 1.dp))
                }
                Spacer(Modifier.height(4.dp))
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Icon(painterResource(R.drawable.ic_calendar), null, tint = Color.White.copy(alpha = 0.85f), modifier = Modifier.size(14.dp))
                    Spacer(Modifier.width(5.dp))
                    Text(todayHeaderDate(), color = Color.White.copy(alpha = 0.9f), fontSize = 11.sp)
                }
            }
            // Live sync pill (tap = upload now)
            Surface(
                onClick = {
                    when {
                        !isOnline -> Toast.makeText(context, "Offline — $pending audio pieces waiting to upload", Toast.LENGTH_SHORT).show()
                        pending > 0 -> {
                            ChunkUploader.scheduleOfflineSync(context)
                            Toast.makeText(context, "Uploading $pending audio pieces…", Toast.LENGTH_SHORT).show()
                        }
                        else -> Toast.makeText(context, "Everything is uploaded", Toast.LENGTH_SHORT).show()
                    }
                },
                color = Color.White.copy(alpha = 0.18f),
                shape = RoundedCornerShape(20.dp)
            ) {
                Row(modifier = Modifier.padding(horizontal = 10.dp, vertical = 7.dp), verticalAlignment = Alignment.CenterVertically) {
                    if (isOnline && pending > 0) {
                        CircularProgressIndicator(modifier = Modifier.size(10.dp), color = Color.White, strokeWidth = 1.5.dp)
                    } else {
                        Box(Modifier.size(8.dp).clip(CircleShape).background(if (isOnline) Color(0xFF4ADE80) else WarningOrange))
                    }
                    Spacer(Modifier.width(6.dp))
                    Text(
                        text = when {
                            !isOnline -> "Offline ($pending)"
                            pending > 0 -> "Syncing ($pending)"
                            else -> "Live sync"
                        },
                        color = Color.White, fontSize = 12.sp, fontWeight = FontWeight.SemiBold, maxLines = 1
                    )
                }
            }
        }
    }
}

/** Meeting / Schedule / Calls in a white card that overlaps the header; locked while a recording runs. */
@Composable
private fun HomeTabs(modes: List<String>, selected: String, onSelect: (String) -> Unit, enabled: Boolean, modifier: Modifier = Modifier) {
    Surface(color = Color.White, shape = RoundedCornerShape(20.dp), shadowElevation = 4.dp, modifier = modifier.fillMaxWidth()) {
        Row(Modifier.padding(8.dp), horizontalArrangement = Arrangement.spacedBy(6.dp)) {
            modes.forEach { m ->
                val sel = m == selected
                val icon = when (m) { "meeting" -> R.drawable.ic_people; "calls" -> R.drawable.ic_phone; else -> R.drawable.ic_calendar }
                val label = when (m) { "calls" -> "Calls"; else -> modeLabel(m) ?: m.replaceFirstChar { it.uppercase() } }
                Surface(
                    onClick = { if (enabled && !sel) onSelect(m) },
                    enabled = enabled || sel,
                    color = if (sel) JomiMint else JomiBg,
                    border = if (sel) BorderStroke(1.dp, JomiMintBorder) else null,
                    shape = RoundedCornerShape(14.dp),
                    modifier = Modifier.weight(1f).height(52.dp)
                ) {
                    Row(horizontalArrangement = Arrangement.Center, verticalAlignment = Alignment.CenterVertically) {
                        val tint = when { sel -> JomiTeal; !enabled -> JomiTextMuted.copy(alpha = 0.5f); else -> JomiTextMuted }
                        Icon(painterResource(icon), null, tint = tint, modifier = Modifier.size(20.dp))
                        Spacer(Modifier.width(6.dp))
                        Text(label, color = tint, fontSize = 14.sp, fontWeight = if (sel) FontWeight.Bold else FontWeight.Medium, maxLines = 1)
                    }
                }
            }
        }
    }
}

/** "ISF Bandhu" card (21 Oct): the assistant is not ready yet, so Ask Bandhu says "Coming soon". */
@Composable
private fun BandhuCard(onAsk: () -> Unit) {
    Surface(color = JomiMint.copy(alpha = 0.6f), shape = RoundedCornerShape(18.dp), border = BorderStroke(1.dp, JomiMintBorder),
        modifier = Modifier.fillMaxWidth()) {
        Row(Modifier.padding(horizontal = 10.dp, vertical = 8.dp), verticalAlignment = Alignment.CenterVertically) {
            Box(Modifier.size(36.dp).clip(CircleShape).background(Color.White), contentAlignment = Alignment.Center) {
                Text("🤖", fontSize = 20.sp)
            }
            Spacer(Modifier.width(10.dp))
            Column(Modifier.weight(1f)) {
                Text("ISF Bandhu", color = JomiTeal, fontWeight = FontWeight.Bold, fontSize = 14.sp)
                Text("Ask specific questions", color = JomiTextMuted, fontSize = 11.sp, maxLines = 1, overflow = TextOverflow.Ellipsis)
            }
            Spacer(Modifier.width(8.dp))
            Surface(onClick = onAsk, color = Color.White, shape = RoundedCornerShape(16.dp), border = BorderStroke(1.dp, JomiMintBorder)) {
                Text("Ask Bandhu →", color = JomiTeal, fontWeight = FontWeight.SemiBold, fontSize = 12.sp,
                    modifier = Modifier.padding(horizontal = 10.dp, vertical = 5.dp))
            }
        }
    }
}

/** Player + status + actions for the recording loaded on Home. */
@Composable
private fun PlayerCard(
    rec: Map<String, Any?>,
    statusItem: Map<String, Any?>?,
    api: ISFApiClient,
    retrying: Boolean,
    onRetry: () -> Unit,
    onReport: () -> Unit,
    onChat: () -> Unit,
    onCopy: () -> Unit,
    onShare: () -> Unit,
    showOwner: Boolean = false,
    onDirectorPov: (() -> Unit)? = null   // admins only: "🎬 Director's POV" next to Report
) {
    val id = rec["id"].toString()
    val title = statusItem?.get("title")?.toString()?.takeIf { it.isNotBlank() && it != "null" } ?: cleanTitle(rec)
    val serverStatus = statusItem?.get("status")?.toString() ?: rec["status"]?.toString()
    val audioReady = serverStatus != null && serverStatus != "recording" && serverStatus != "uploading"
    val ui = transcriptionUiFrom(statusItem ?: rec.takeIf { it["status"] != null })
    JomiCard(padding = 16.dp) {
        Text("Now playing", color = JomiTeal, fontSize = 11.sp, fontWeight = FontWeight.Bold, letterSpacing = 1.sp)
        Text(title, fontWeight = FontWeight.SemiBold, fontSize = 16.sp, maxLines = 2, overflow = TextOverflow.Ellipsis)
        val meta = listOfNotNull(recordingDate(rec).takeIf { it.isNotBlank() }, modeLabel(rec["mode"]), recordingPeopleLine(rec))
        if (meta.isNotEmpty()) Text(meta.joinToString(" · "), color = JomiTextMuted, fontSize = 12.sp, maxLines = 2, overflow = TextOverflow.Ellipsis)
        if (showOwner) (rec["user_name"] ?: rec["recorded_by_name"])?.toString()?.takeIf { it.isNotBlank() && it != "null" }?.let {
            Text("🎙️ Recorded by $it", color = JomiTeal, fontSize = 13.sp, fontWeight = FontWeight.SemiBold, maxLines = 1, overflow = TextOverflow.Ellipsis)
        }
        (silenceSkippedLine(statusItem) ?: silenceSkippedLine(rec))?.let { Text(it, color = JomiTextMuted, fontSize = 12.sp) }
        Spacer(Modifier.height(12.dp))
        if (audioReady) {
            val controller = rememberAudioPlayerController(api.audioUrl(id), audioWasCleanedUp(rec))
            AudioPlayer(controller = controller, downloadName = title, fallbackDurationSec = recordingSeconds(rec))
        } else {
            Row(verticalAlignment = Alignment.CenterVertically) {
                CircularProgressIndicator(modifier = Modifier.size(14.dp), color = JomiGreen, strokeWidth = 2.dp)
                Spacer(Modifier.width(8.dp))
                Text("Preparing audio playback…", color = JomiTextMuted, fontSize = 13.sp)
            }
        }
    }
    Spacer(Modifier.height(12.dp))
    TranscriptionStatusCard(state = ui, retrying = retrying, onRetry = onRetry, onOpenReport = onReport)
    Spacer(Modifier.height(10.dp))
    val done = ui is TranscriptionUi.Done && !ui.noSpeech
    if (onDirectorPov != null) {
        // Admins: Report and Director's POV side by side; AI Chat / Copy / Share on the next row
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp), modifier = Modifier.fillMaxWidth()) {
            ActionChipButton("Report", R.drawable.ic_report, onReport, modifier = Modifier.weight(1f))
            ActionChipButton("🎬 Director's POV", null, onDirectorPov, modifier = Modifier.weight(1f))
        }
        Spacer(Modifier.height(8.dp))
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp), modifier = Modifier.fillMaxWidth()) {
            ActionChipButton("AI Chat", R.drawable.ic_chat, onChat, modifier = Modifier.weight(1f),
                tint = MaterialTheme.colorScheme.onBackground, container = Color.White, border = JomiBorder)
            ActionChipButton("Copy", R.drawable.ic_copy, onCopy, enabled = done, modifier = Modifier.weight(1f),
                tint = JomiTextMuted, container = Color.White, border = JomiBorder)
            ActionChipButton("Share", R.drawable.ic_share, onShare, enabled = done, modifier = Modifier.weight(1f),
                tint = JomiTextMuted, container = Color.White, border = JomiBorder)
        }
        return
    }
    Row(horizontalArrangement = Arrangement.spacedBy(8.dp), modifier = Modifier.fillMaxWidth()) {
        ActionChipButton("Report", R.drawable.ic_report, onReport, modifier = Modifier.weight(1f))
        ActionChipButton("AI Chat", R.drawable.ic_chat, onChat, modifier = Modifier.weight(1f),
            tint = MaterialTheme.colorScheme.onBackground, container = Color.White, border = JomiBorder)
    }
    Spacer(Modifier.height(8.dp))
    Row(horizontalArrangement = Arrangement.spacedBy(8.dp), modifier = Modifier.fillMaxWidth()) {
        ActionChipButton("Copy", R.drawable.ic_copy, onCopy, enabled = done, modifier = Modifier.weight(1f),
            tint = JomiTextMuted, container = Color.White, border = JomiBorder)
        ActionChipButton("Share", R.drawable.ic_share, onShare, enabled = done, modifier = Modifier.weight(1f),
            tint = JomiTextMuted, container = Color.White, border = JomiBorder)
    }
}

/**
 * The recorder, fixed at the bottom of Home (21 Oct): Balgruha + Assistant coach (or Meeting name + Participants)
 * side by side, mic status, timer, Pause · Record · Stop. While recording it also shows the waveform and status.
 */
@Composable
private fun RecorderPanel(
    mode: String,
    cfg: RecorderConfig,
    vm: HomeViewModel,
    session: RecordingSessionInfo?,
    starting: Boolean,
    finished: FinishedSession?,
    mic: MicReadiness,
    externalMic: com.isf.recorder.data.model.MicType?,
    amplitude: Float,
    canStart: Boolean,
    locked: Boolean,
    onRecord: () -> Unit,
    onPause: () -> Unit,
    onResume: () -> Unit,
    onStop: () -> Unit,
    onUsePhoneMic: () -> Unit,
    expanded: Boolean,
    onToggle: () -> Unit,
    modifier: Modifier = Modifier
) {
    val status = session?.status
    val isRecording = status == RecordingStatus.RECORDING
    val isPaused = status == RecordingStatus.PAUSED_USER || status == RecordingStatus.PAUSED_CONFLICT
    val isFinalizing = status == RecordingStatus.FINALIZING
    val showFinished = session == null && !starting && finished != null

    Surface(
        color = Color.White,
        shape = RoundedCornerShape(topStart = 28.dp, topEnd = 28.dp),
        shadowElevation = 16.dp,
        modifier = modifier.fillMaxWidth()
    ) {
        Column(Modifier.padding(start = 16.dp, end = 16.dp, top = 8.dp, bottom = 12.dp)) {
            // Handle + ⌃ / ⌄: opens the details (mic, Balgruha / coach, timer); closed = only the buttons
            Row(
                Modifier.fillMaxWidth().clickable(onClick = onToggle).padding(vertical = 2.dp),
                horizontalArrangement = Arrangement.Center, verticalAlignment = Alignment.CenterVertically
            ) {
                Box(Modifier.width(40.dp).height(4.dp).clip(RoundedCornerShape(2.dp)).background(JomiBorder))
                Spacer(Modifier.width(8.dp))
                Icon(
                    if (expanded) Icons.Filled.KeyboardArrowDown else Icons.Filled.KeyboardArrowUp,
                    contentDescription = if (expanded) "Hide details" else "Show Balgruha, coach and timer",
                    tint = JomiTeal, modifier = Modifier.size(22.dp)
                )
                Text(if (expanded) "Hide" else "Details", color = JomiTeal, fontSize = 12.sp, fontWeight = FontWeight.SemiBold)
            }
            Spacer(Modifier.height(6.dp))

            // Urgent: always shown, also when closed
            if (session?.micLost == true) {
                MicLostPrompt(message = session.micMessage, onUsePhoneMic = onUsePhoneMic, onStop = onStop)
                Spacer(Modifier.height(8.dp))
            } else if (expanded) {
                MicStatusBar(externalMic = externalMic, activeMic = session?.micType)
                Spacer(Modifier.height(10.dp))
            }
            session?.micMessage?.takeIf { it.isNotBlank() && !session.micLost }?.let {
                MessageBanner(it, color = if (it == AudioRecordingService.MSG_MIC_CONNECTED) JomiGreen else WarningOrange)
                Spacer(Modifier.height(8.dp))
            }
            if (status == RecordingStatus.PAUSED_CONFLICT && session?.micMessage.isNullOrBlank()) {
                MessageBanner("Paused — the microphone is busy (call). Tap Resume when the call ends.")
                Spacer(Modifier.height(8.dp))
            }

            AnimatedVisibility(visible = expanded) {
            Column {
            // Who / where: two fields side by side
            Row(verticalAlignment = Alignment.Bottom) {
                if (mode == "schedule") {
                    SinglePicker(
                        label = "Balgruha name", sheetTitle = "Select Balgruha", searchHint = "Search Balgruha…",
                        options = cfg.balgruhs, selected = vm.balgruh, onSelect = { vm.balgruh = it },
                        enabled = !locked, allowCustom = cfg.balgruhs.isEmpty(), modifier = Modifier.weight(1f)
                    )
                    Spacer(Modifier.width(10.dp))
                    MultiPicker(
                        label = "Assistant ${cfg.roleLabel.lowercase()}", sheetTitle = "Select Assistant ${cfg.roleLabel.lowercase()}",
                        searchHint = "Search ${cfg.roleLabel.lowercase()}…", options = cfg.coaches, selected = vm.coaches,
                        onChange = { vm.coaches = it }, enabled = !locked,
                        subtitle = { "Assistant ${cfg.roleLabel.lowercase()}" }, modifier = Modifier.weight(1f)
                    )
                } else {
                    Column(Modifier.weight(1f)) {
                        Text("Meeting name", color = MaterialTheme.colorScheme.onBackground, fontSize = 13.sp, fontWeight = FontWeight.SemiBold)
                        Spacer(Modifier.height(6.dp))
                        OutlinedTextField(
                            value = vm.meetingName, onValueChange = { vm.meetingName = it }, enabled = !locked, singleLine = true,
                            placeholder = { Text("Optional", color = JomiTextMuted, fontSize = 14.sp) },
                            textStyle = LocalTextStyle.current.copy(fontSize = 14.sp),
                            shape = RoundedCornerShape(12.dp),
                            colors = OutlinedTextFieldDefaults.colors(
                                focusedContainerColor = Color.White, unfocusedContainerColor = Color.White, disabledContainerColor = Color.White,
                                focusedBorderColor = JomiGreen, unfocusedBorderColor = JomiBorder
                            ),
                            modifier = Modifier.fillMaxWidth().heightIn(min = 48.dp)
                        )
                    }
                    Spacer(Modifier.width(10.dp))
                    MultiPicker(
                        label = "Participants", sheetTitle = "Select participants", searchHint = "Search name…",
                        options = cfg.participants, selected = vm.participants, onChange = { vm.participants = it },
                        enabled = !locked, modifier = Modifier.weight(1f)
                    )
                }
            }
            Spacer(Modifier.height(10.dp))

            if (session != null) {
                WaveformVisualizer(
                    isRecording = isRecording && session.voiceActive,
                    isPaused = isPaused || (isRecording && !session.voiceActive),
                    amplitude = amplitude, barCount = 44,
                    modifier = Modifier.fillMaxWidth().height(30.dp)
                )
                Spacer(Modifier.height(6.dp))
            }

            val seconds = session?.durationSeconds ?: if (showFinished) finished!!.voiceSeconds else 0
            Surface(color = JomiMint, shape = RoundedCornerShape(16.dp), modifier = Modifier.align(Alignment.CenterHorizontally)) {
                Text(clockHms(seconds), color = MaterialTheme.colorScheme.onBackground, fontSize = 18.sp, fontWeight = FontWeight.Bold,
                    letterSpacing = 1.sp, modifier = Modifier.padding(horizontal = 16.dp, vertical = 4.dp))
            }
            val statusLine = when {
                starting && session == null -> "Starting…"
                isFinalizing -> "Saving…"
                status == RecordingStatus.PAUSED_CONFLICT -> "Paused — microphone busy"
                status == RecordingStatus.PAUSED_USER -> "Paused"
                isRecording && session?.voiceActive == false -> "Listening — silence not saved"
                isRecording -> "Recording…"
                showFinished && !finished!!.uploadPending -> "Recording saved · ${humanDuration(finished.voiceSeconds)}"
                showFinished -> "Saved on phone — will upload when online"
                session == null && mic != MicReadiness.READY -> mic.label
                !canStart && mode == "schedule" -> "Choose the Balgruha and at least one Assistant coach"
                !canStart -> "Add at least one participant"
                else -> null      // ready: nothing to say
            }
            statusLine?.let {
                Text(it, color = if (session == null && mic != MicReadiness.READY) WarningOrange else JomiTextMuted, fontSize = 12.sp,
                    textAlign = TextAlign.Center, modifier = Modifier.fillMaxWidth().padding(top = 4.dp))
            }
            Spacer(Modifier.height(8.dp))
            }
            }

            Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceEvenly, verticalAlignment = Alignment.CenterVertically) {
                RoundControl(
                    icon = if (isPaused) R.drawable.ic_play else R.drawable.ic_pause,
                    label = if (isPaused) "Resume" else "Pause",
                    enabled = isRecording || isPaused,
                    style = if (isPaused) ControlStyle.Primary else ControlStyle.Neutral,
                    onClick = { if (isPaused) onResume() else onPause() },
                    size = 50.dp
                )
                RoundControl(
                    icon = R.drawable.ic_mic,
                    label = if (isRecording) "Recording" else "Record",
                    // not ready (Balgruha / coach missing): the tap opens the details so they can be chosen
                    enabled = canStart || (session == null && !starting && !locked),
                    style = if (session != null || starting) ControlStyle.Live else ControlStyle.Primary,
                    pulsing = isRecording,
                    onClick = { if (canStart) onRecord() else if (!expanded) onToggle() },
                    size = 64.dp
                )
                RoundControl(
                    icon = R.drawable.ic_stop,
                    label = if (isFinalizing) "Saving…" else "Stop",
                    enabled = isRecording || isPaused,
                    style = ControlStyle.Danger,
                    busy = isFinalizing,
                    onClick = onStop,
                    size = 50.dp
                )
            }
        }
    }
}

private enum class ControlStyle { Primary, Live, Neutral, Danger }

@Composable
private fun RoundControl(
    icon: Int,
    label: String,
    enabled: Boolean,
    style: ControlStyle,
    onClick: () -> Unit,
    pulsing: Boolean = false,
    busy: Boolean = false,
    size: androidx.compose.ui.unit.Dp = 64.dp
) {
    val transition = rememberInfiniteTransition(label = "pulse")
    val pulse by transition.animateFloat(
        initialValue = 1f,
        targetValue = if (pulsing) 1.08f else 1f,
        animationSpec = infiniteRepeatable(tween(800, easing = FastOutSlowInEasing), RepeatMode.Reverse),
        label = "pulseScale"
    )
    val (bg, fg, border) = when {
        style == ControlStyle.Live -> Triple(RecordRed, Color.White, Color.Transparent)
        !enabled -> Triple(Color(0xFFF3F4F6), Color(0xFFC4C9D0), if (style == ControlStyle.Danger) RecordRed.copy(alpha = 0.15f) else Color.Transparent)
        style == ControlStyle.Primary -> Triple(JomiGreen, Color.White, Color.Transparent)
        style == ControlStyle.Danger -> Triple(RecordRed.copy(alpha = 0.08f), RecordRed, RecordRed.copy(alpha = 0.35f))
        else -> Triple(JomiMint, JomiTeal, JomiMintBorder)
    }
    Column(horizontalAlignment = Alignment.CenterHorizontally) {
        Surface(
            onClick = onClick,
            enabled = enabled && !busy,
            shape = CircleShape,
            color = bg,
            border = BorderStroke(1.5.dp, border),
            shadowElevation = if (enabled && style == ControlStyle.Primary) 3.dp else 0.dp,
            modifier = Modifier.size(size).scale(pulse)
        ) {
            Box(contentAlignment = Alignment.Center) {
                if (busy) {
                    CircularProgressIndicator(modifier = Modifier.size(24.dp), color = RecordRed, strokeWidth = 2.dp)
                } else {
                    Icon(painterResource(icon), contentDescription = label, tint = fg, modifier = Modifier.size(26.dp))
                }
            }
        }
        Spacer(Modifier.height(6.dp))
        Text(label, color = if (enabled || style == ControlStyle.Live) MaterialTheme.colorScheme.onBackground else JomiTextMuted, fontSize = 13.sp)
    }
}

/** mm:ss or hh:mm:ss — kept here for the older screens that use it. */
fun formatDuration(totalSec: Int): String {
    val hrs = totalSec / 3600
    val mins = (totalSec % 3600) / 60
    val secs = totalSec % 60
    return if (hrs > 0) String.format("%02d:%02d:%02d", hrs, mins, secs) else String.format("%02d:%02d", mins, secs)
}

/**
 * Filters under "Previous recordings", all on one line (14 Oct): Balgruh · Coach (admin) or Asst. coach
 * (coach) · Date. Balgruh and assistant coach only exist for Schedule recordings, so they are hidden on
 * the Meeting tab.
 */
@Composable
private fun RecordingFilters(vm: HomeViewModel, cfg: RecorderConfig, isAdmin: Boolean, schedule: Boolean) {
    // admins: Coach = who RECORDED the session, so only people with a Jomi login (they are the ones who record)
    val people = if (isAdmin) cfg.coaches.filter { it.userId != null } else cfg.coaches
    Row(horizontalArrangement = Arrangement.spacedBy(8.dp), modifier = Modifier.fillMaxWidth()) {
        if (schedule) {
            FilterChipMenu("Balgruha", vm.fBalgruh, cfg.balgruhs.map { it.name }, { vm.setFilters(balgruh = it) },
                modifier = Modifier.weight(1f))
        }
        if (isAdmin || schedule) {
            FilterChipMenu(if (isAdmin) "Coach" else "Asst. coach", vm.fPerson?.name, people.map { it.name },
                { name -> vm.setFilters(person = people.firstOrNull { it.name == name }) }, modifier = Modifier.weight(1f))
        }
        DateFilterChip(vm.allDate, { vm.changeAllDate(it) }, modifier = Modifier.weight(1f))
    }
    if (vm.filtersActive) {
        TextButton(onClick = { vm.clearFilters() }, contentPadding = PaddingValues(horizontal = 4.dp, vertical = 0.dp)) {
            Text("✕ Clear filters", color = JomiTeal, fontSize = 13.sp)
        }
    }
}
