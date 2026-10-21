package com.isf.recorder.ui.screens

import android.widget.Toast
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.isf.recorder.R
import com.isf.recorder.data.api.ISFApiClient
import com.isf.recorder.data.prefs.UserPreferences
import com.isf.recorder.ui.components.*
import com.isf.recorder.ui.theme.*
import kotlinx.coroutines.launch

/**
 * Admin "All recordings" page (round 2): person dropdown + date dropdown
 * (All / Today / Yesterday / Custom range), paging, long-press → Merge / Delete.
 * Tap = transcript & versions; Report = the report popup.
 */
@Composable
fun RecordingsListScreen(
    onSelectRecording: (String) -> Unit,
    onOpenReport: (String) -> Unit,
    onOpenChat: (recordingId: String, title: String) -> Unit,
    onBack: () -> Unit
) {
    val context = LocalContext.current
    val api = remember { ISFApiClient(context) }
    val prefs = remember { UserPreferences(context) }
    val scope = rememberCoroutineScope()
    val isAdmin = prefs.isAdmin

    var recordings by remember { mutableStateOf<List<Map<String, Any?>>>(emptyList()) }
    var isLoading by remember { mutableStateOf(true) }
    var loadError by remember { mutableStateOf<String?>(null) }
    var currentPage by remember { mutableIntStateOf(1) }
    var totalCount by remember { mutableIntStateOf(0) }
    var isLoadingMore by remember { mutableStateOf(false) }

    // Filters survive opening a recording and coming back (kept for the app session, see AllRecordingsFilters)
    var dateFilter by remember { mutableStateOf(AllRecordingsFilters.date) }
    var people by remember { mutableStateOf<List<Map<String, Any?>>>(emptyList()) }
    var person by remember { mutableStateOf(AllRecordingsFilters.person) }

    var selectedIds by remember { mutableStateOf(setOf<String>()) }
    val selectionMode = selectedIds.isNotEmpty()
    var mergeTargets by remember { mutableStateOf<List<Map<String, Any?>>?>(null) }
    var eraseTargets by remember { mutableStateOf<List<Map<String, Any?>>?>(null) }
    var editTarget by remember { mutableStateOf<Map<String, Any?>?>(null) }
    var reportFor by remember { mutableStateOf<Map<String, Any?>?>(null) }
    var mergeRunning by remember { mutableStateOf(false) }

    suspend fun fetch(page: Int) = api.listRecordings(
        page = page,
        limit = 50,
        userId = if (isAdmin) person?.userId else null,
        dateFrom = dateFilter.fromParam,
        dateTo = dateFilter.toParam
    )

    fun loadData() {
        isLoading = true
        scope.launch {
            val res = fetch(1)
            isLoading = false
            res.onSuccess { data ->
                @Suppress("UNCHECKED_CAST")
                val items = data["items"] as? List<Map<String, Any?>> ?: emptyList()
                totalCount = (data["total"] as? Number)?.toInt() ?: items.size
                recordings = items
                currentPage = 1
                loadError = null
            }.onFailure { loadError = it.message }
        }
    }

    fun loadMore() {
        if (isLoadingMore || recordings.size >= totalCount) return
        isLoadingMore = true
        scope.launch {
            val next = currentPage + 1
            val res = fetch(next)
            isLoadingMore = false
            res.onSuccess { data ->
                @Suppress("UNCHECKED_CAST")
                val items = data["items"] as? List<Map<String, Any?>> ?: emptyList()
                if (items.isNotEmpty()) { recordings = recordings + items; currentPage = next }
            }.onFailure { Toast.makeText(context, it.message ?: "Could not load more", Toast.LENGTH_SHORT).show() }
        }
    }

    LaunchedEffect(Unit) { if (isAdmin) api.listUsers().onSuccess { people = it } }
    LaunchedEffect(person, dateFilter) { loadData() }

    SlideBackContainer(onBack = { if (selectionMode) selectedIds = emptySet() else onBack() }) {
        Box(Modifier.fillMaxSize().background(MaterialTheme.colorScheme.background)) {
            Column(Modifier.fillMaxSize().padding(horizontal = 16.dp, vertical = 12.dp)) {
                BackHeader(
                    title = if (isAdmin) "All recordings" else "My recordings",
                    subtitle = "$totalCount recordings",
                    onBack = onBack
                ) {
                    SmallIconButton(R.drawable.ic_refresh, "Refresh", { loadData() })
                }
                Spacer(Modifier.height(10.dp))
                Row(horizontalArrangement = Arrangement.spacedBy(10.dp), modifier = Modifier.fillMaxWidth()) {
                    if (isAdmin) {
                        val everyone = NameOption("Everyone", null)
                        SelectDropdown(
                            label = "Person",
                            options = listOf(everyone) + people.mapNotNull { u ->
                                val id = u["id"]?.toString() ?: return@mapNotNull null
                                NameOption(u["name"]?.toString()?.ifBlank { null } ?: u["phone"]?.toString() ?: "Unknown", id)
                            },
                            selected = person ?: everyone,
                            onSelect = { person = it.takeIf { o -> o.userId != null }; AllRecordingsFilters.person = person },
                            modifier = Modifier.weight(1f)
                        )
                    }
                    DateDropdown(dateFilter, { dateFilter = it; AllRecordingsFilters.date = it }, modifier = Modifier.weight(1f))
                }
                if (mergeRunning) {
                    Spacer(Modifier.height(6.dp))
                    LinearProgressIndicator(color = JomiGreen, modifier = Modifier.fillMaxWidth())
                }
                Spacer(Modifier.height(10.dp))

                when {
                    isLoading -> Box(Modifier.fillMaxWidth().weight(1f), contentAlignment = Alignment.Center) {
                        CircularProgressIndicator(color = JomiGreen)
                    }
                    recordings.isEmpty() -> Box(Modifier.fillMaxWidth().weight(1f), contentAlignment = Alignment.Center) {
                        Text(loadError?.let { "Could not load: $it" } ?: "No recordings for these filters.", color = JomiTextMuted, fontSize = 14.sp)
                    }
                    else -> LazyColumn(
                        verticalArrangement = Arrangement.spacedBy(12.dp),
                        contentPadding = PaddingValues(bottom = if (selectionMode) 96.dp else 16.dp),
                        modifier = Modifier.weight(1f)
                    ) {
                        items(recordings, key = { it["id"]?.toString() ?: it.hashCode().toString() }) { rec ->
                            val id = rec["id"]?.toString().orEmpty()
                            RecordingCard(
                                rec = rec,
                                showOwner = isAdmin,
                                onOpen = { onSelectRecording(id) },
                                onReport = { reportFor = rec },
                                onChat = { onOpenChat(id, cleanTitle(rec)) },
                                onCopy = { scope.launch { copyOrShareRecording(context, api, rec, share = false) } },
                                onShare = { scope.launch { copyOrShareRecording(context, api, rec, share = true) } },
                                onEditTitle = { editTarget = rec },
                                onDelete = { eraseTargets = listOf(rec) },
                                selectionMode = selectionMode,
                                selected = id in selectedIds,
                                onToggleSelect = { selectedIds = if (id in selectedIds) selectedIds - id else selectedIds + id },
                                onLongPress = { selectedIds = selectedIds + id }
                            )
                        }
                        if (recordings.size < totalCount) {
                            item {
                                Box(Modifier.fillMaxWidth().padding(vertical = 12.dp), contentAlignment = Alignment.Center) {
                                    if (isLoadingMore) {
                                        CircularProgressIndicator(Modifier.size(24.dp), color = JomiGreen, strokeWidth = 2.dp)
                                    } else {
                                        OutlinedButton(onClick = { loadMore() }, border = BorderStroke(1.dp, JomiMintBorder)) {
                                            Text("Load older (${recordings.size} of $totalCount)", color = JomiTeal)
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }
            if (selectionMode) {
                SelectionActionBar(
                    count = selectedIds.size,
                    onMerge = { mergeTargets = recordings.filter { it["id"]?.toString() in selectedIds } },
                    onDelete = { eraseTargets = recordings.filter { it["id"]?.toString() in selectedIds } },
                    onCancel = { selectedIds = emptySet() },
                    modifier = Modifier.align(Alignment.BottomCenter).padding(12.dp)
                )
            }
        }
    }

    reportFor?.let { rec ->
        val id = rec["id"]?.toString().orEmpty()
        ReportSheet(
            recordingId = id,
            title = cleanTitle(rec),
            onDismiss = { reportFor = null },
            onOpenFull = if (isAdmin) ({ reportFor = null; onOpenReport(id) }) else null
        )
    }
    mergeTargets?.let { recs ->
        MergeDialog(
            api, recs,
            onDismiss = { mergeTargets = null },
            onStarted = { mergeTargets = null; selectedIds = emptySet(); mergeRunning = true },
            onMerged = { newId ->
                mergeRunning = false
                loadData()
                newId?.takeIf { it.isNotBlank() }?.let(onSelectRecording)
            }
        )
    }
    eraseTargets?.let { recs ->
        BulkEraseDialog(api, recs, onDismiss = { eraseTargets = null }, onDone = {
            eraseTargets = null
            selectedIds = emptySet()
            loadData()
        })
    }
    editTarget?.let { rec ->
        EditTitleDialog(
            initial = cleanTitle(rec),
            onDismiss = { editTarget = null },
            onSave = { t ->
                val res = api.updateRecordingTitle(rec["id"].toString(), t)
                if (res.isSuccess) { loadData(); null } else res.exceptionOrNull()?.message ?: "Could not rename"
            }
        )
    }
}

/** The admin All-recordings filters, kept in memory for the app session so they are not lost when a
 *  recording is opened and the user comes back (the screen itself is rebuilt on every visit). */
object AllRecordingsFilters {
    var date: DateFilter = DateFilter.ALL
    var person: NameOption? = null
}
