package com.isf.recorder.ui.screens

import android.app.Application
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.isf.recorder.data.api.ISFApiClient
import com.isf.recorder.data.prefs.UserPreferences
import com.isf.recorder.sync.ReportStatusWorker
import com.isf.recorder.ui.components.NameOption
import com.isf.recorder.ui.components.TranscriptionUi
import com.isf.recorder.ui.components.isActive
import com.isf.recorder.ui.components.transcriptionUiFrom
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch

/** Parsed GET /settings/recorder-config (contract §3.1). */
data class RecorderConfig(
    val roleLabel: String = UserPreferences.DEFAULT_ROLE_LABEL,
    val modes: List<String> = listOf("meeting", "schedule"),
    val defaultMode: String = "schedule",
    val balgruhs: List<NameOption> = emptyList(),
    val coaches: List<NameOption> = emptyList(),
    val participants: List<NameOption> = emptyList(),
    val reportTemplates: Map<String, Pair<String, String>> = emptyMap(), // mode -> (id, title)
    val silenceStopMinutes: Map<String, Int> = emptyMap(),               // mode -> minutes without voice before auto-stop (0 = off)
    val loaded: Boolean = false
) {
    companion object {
        @Suppress("UNCHECKED_CAST")
        fun from(map: Map<String, Any?>?): RecorderConfig {
            if (map == null) return RecorderConfig()
            fun names(key: String): List<NameOption> =
                (map[key] as? List<*>)?.mapNotNull { item ->
                    val m = item as? Map<String, Any?> ?: return@mapNotNull null
                    val name = m["name"]?.toString()?.trim().orEmpty()
                    if (name.isEmpty()) null else NameOption(name, m["user_id"]?.toString()?.takeIf { it.isNotBlank() && it != "null" })
                }?.distinctBy { it.name.lowercase() } ?: emptyList()
            // Meeting first, Schedule second — matches the mockup toggle order.
            val modes = (map["modes"] as? List<*>)?.map { it.toString() }?.filter { it == "schedule" || it == "meeting" }
                ?.sortedBy { if (it == "meeting") 0 else 1 }?.takeIf { it.isNotEmpty() } ?: listOf("meeting", "schedule")
            val templates = (map["report_templates"] as? Map<String, Any?>)?.mapNotNull { (mode, v) ->
                val m = v as? Map<String, Any?> ?: return@mapNotNull null
                val id = m["id"]?.toString() ?: return@mapNotNull null
                mode to (id to (m["title"]?.toString() ?: ""))
            }?.toMap() ?: emptyMap()
            return RecorderConfig(
                roleLabel = map["role_label"]?.toString()?.takeIf { it.isNotBlank() } ?: UserPreferences.DEFAULT_ROLE_LABEL,
                modes = modes,
                defaultMode = map["default_mode"]?.toString()?.takeIf { it in modes } ?: if ("schedule" in modes) "schedule" else modes.first(),
                balgruhs = names("balgruhs"),
                coaches = names("coaches"),
                participants = names("participants"),
                reportTemplates = templates,
                silenceStopMinutes = (map["silence_stop_minutes"] as? Map<*, *>)
                    ?.mapNotNull { (k, v) -> (v as? Number)?.let { k.toString() to it.toInt() } }?.toMap() ?: emptyMap(),
                loaded = true
            )
        }
    }
}

/**
 * Activity-scoped state for Home so selections survive a trip to Settings and back
 * (but are cleared after every recording — never remembered between recordings).
 */
class HomeViewModel(app: Application) : AndroidViewModel(app) {
    private val api = ISFApiClient(app)
    private val prefs = UserPreferences(app)

    var config by mutableStateOf(RecorderConfig.from(api.cachedRecorderConfig()))
        private set
    var configError by mutableStateOf<String?>(null)
        private set

    // Selections for the next recording
    var mode by mutableStateOf<String?>(null)
    var balgruh by mutableStateOf<NameOption?>(null)
    /** One or more assistant coaches (round 2). Nothing is pre-selected. */
    var coaches by mutableStateOf<List<NameOption>>(emptyList())
    var meetingName by mutableStateOf("")
    var participants by mutableStateOf<List<NameOption>>(emptyList())

    /** Record tapped; true until the service publishes a session (or times out). */
    var starting by mutableStateOf(false)

    // Previous recordings (own, top 5)
    var recent by mutableStateOf<List<Map<String, Any?>>>(emptyList())
        private set
    var recentLoading by mutableStateOf(false)
        private set
    var recentError by mutableStateOf<String?>(null)
        private set

    // Status of the last finished recording
    var watchedId: String? = null
        private set
    var statusItem by mutableStateOf<Map<String, Any?>?>(null)
        private set
    var retrying by mutableStateOf(false)
        private set
    private var pollJob: Job? = null
    private var hadSession = false

    val effectiveMode: String get() = mode ?: config.defaultMode

    /** Recording loaded into the Home player (tapped in the list); null = the last finished one. */
    var selected by mutableStateOf<Map<String, Any?>?>(null)
        private set

    fun select(rec: Map<String, Any?>?) { selected = rec }

    /** Set by the "Report ready" deep link: Home loads that recording and opens its report popup. */
    var pendingReportId by mutableStateOf<String?>(null)

    private var lastFinishedId: String? = null

    /** A newly finished recording becomes the player's default again (not on screen re-entry). */
    fun onFinished(recordingId: String?) {
        if (recordingId != lastFinishedId) {
            lastFinishedId = recordingId
            if (recordingId != null) selected = null
        }
    }

    /** Home → Calls tab selected (14 Oct): shows my synced phone calls instead of the recorder. */
    var callsTab by mutableStateOf(false)

    // "See all" expanded in place (coaches)
    var allExpanded by mutableStateOf(false)
    var allDate by mutableStateOf(com.isf.recorder.ui.components.DateFilter.ALL)
        private set
    var allItems by mutableStateOf<List<Map<String, Any?>>>(emptyList())
        private set
    var allTotal by mutableStateOf(0)
        private set
    var allLoading by mutableStateOf(false)
        private set
    private var allPage = 1

    // Filters on "Previous recordings" (13 Oct): Balgruh + Coach (admin) or Assistant coach (coach) + Date.
    // They apply to the 3 recent cards and to "See all" (which opens in place for everyone).
    var fBalgruh by mutableStateOf<String?>(null)
        private set
    var fPerson by mutableStateOf<NameOption?>(null)
        private set
    val filtersActive: Boolean
        get() = fBalgruh != null || fPerson != null || allDate != com.isf.recorder.ui.components.DateFilter.ALL

    fun setFilters(
        balgruh: String? = fBalgruh,
        person: NameOption? = fPerson,
        date: com.isf.recorder.ui.components.DateFilter = allDate
    ) {
        fBalgruh = balgruh; fPerson = person; allDate = date
        loadRecent()
        if (allExpanded) loadAll(reset = true)
    }

    fun clearFilters() = setFilters(null, null, com.isf.recorder.ui.components.DateFilter.ALL)

    /** Admins see every coach's recordings (optionally one coach); coaches only their own. */
    private fun listUserId(): String? = if (prefs.isAdmin) fPerson?.userId else prefs.userId
    private fun listAssistantCoach(): String? = if (prefs.isAdmin) null else fPerson?.name

    fun changeAllDate(f: com.isf.recorder.ui.components.DateFilter) = setFilters(date = f)

    fun loadAll(reset: Boolean) {
        prefs.userId ?: return
        if (allLoading) return
        if (!reset && allItems.size >= allTotal) return
        val page = if (reset) 1 else allPage + 1
        allLoading = true
        viewModelScope.launch {
            val res = api.listRecordings(page = page, limit = 20, userId = listUserId(), dateFrom = allDate.fromParam,
                dateTo = allDate.toParam, mode = listMode, balgruh = fBalgruh, assistantCoach = listAssistantCoach())
            allLoading = false
            res.onSuccess { data ->
                @Suppress("UNCHECKED_CAST")
                val items = (data["items"] as? List<Map<String, Any?>>) ?: emptyList()
                allTotal = (data["total"] as? Number)?.toInt() ?: items.size
                allItems = if (reset) items else allItems + items
                allPage = page
            }
        }
    }

    /** Reload whichever lists are visible (after rename / erase / merge). */
    fun reloadLists() {
        loadRecent()
        if (allExpanded) loadAll(reset = true)
    }

    /** Called each time Home is shown (also after a different user signs in). */
    fun refresh() {
        loadConfig()
        loadRecent()
        // Daily task reminder: the setting lives on the server; (re)plan the phone notification from it
        viewModelScope.launch {
            api.getTaskReminder().onSuccess { r ->
                com.isf.recorder.sync.TaskReminder.schedule(getApplication(), r["on"] as? Boolean ?: true,
                    r["time"]?.toString() ?: "09:00")
            }
        }
    }

    fun loadConfig() {
        viewModelScope.launch {
            api.getRecorderConfig()
                .onSuccess { config = RecorderConfig.from(it); configError = null }
                .onFailure { configError = it.message }
        }
    }

    /** The Meeting / Schedule tab: Previous recordings and See all show only that mode. */
    var listMode by mutableStateOf<String?>(null)
        private set

    fun setListModeTo(m: String?) {
        if (m == listMode) return
        listMode = m
        recent = emptyList()
        loadRecent()
        // a new tab starts collapsed: 3 recent + "See all", without filters
        allExpanded = false
        allItems = emptyList()
        fBalgruh = null; fPerson = null; allDate = com.isf.recorder.ui.components.DateFilter.ALL
    }

    fun loadRecent() {
        prefs.userId ?: return
        recentLoading = recent.isEmpty()
        viewModelScope.launch {
            val res = api.listRecordings(page = 1, limit = RECENT_COUNT, userId = listUserId(), mode = listMode,
                dateFrom = allDate.fromParam, dateTo = allDate.toParam, balgruh = fBalgruh, assistantCoach = listAssistantCoach())
            recentLoading = false
            res.onSuccess { data ->
                @Suppress("UNCHECKED_CAST")
                recent = (data["items"] as? List<Map<String, Any?>>) ?: emptyList()
                recentError = null
            }.onFailure { recentError = it.message }
        }
    }

    /** Called whenever "is a session active" changes; clears selections when a session ends. */
    fun onSessionActive(active: Boolean) {
        if (active) {
            hadSession = true
            starting = false
        } else if (hadSession) {
            hadSession = false
            resetSelections()
            loadRecent()
        }
    }

    fun resetSelections() {
        mode = null
        balgruh = null
        coaches = emptyList()
        meetingName = ""
        participants = emptyList()
    }

    /** Poll /recordings/status at once, then every 3 s for [recordingId] until it is done or failed. */
    fun watch(recordingId: String?) {
        if (recordingId == watchedId) return
        watchedId = recordingId
        statusItem = null
        pollJob?.cancel()
        if (recordingId.isNullOrBlank()) return
        pollJob = viewModelScope.launch {
            var idleDonePolls = 0
            while (isActive) {
                fetchStatus(recordingId)?.let { statusItem = it }
                val ui = transcriptionUiFrom(statusItem)
                if (!ui.isActive() && statusItem != null) {
                    // The report row may appear a moment after "completed": give it ~15 s.
                    if (ui is TranscriptionUi.Done && !ui.hasReport && !ui.noSpeech && idleDonePolls < 5) {
                        idleDonePolls++
                    } else {
                        loadRecent()
                        break
                    }
                }
                delay(STATUS_POLL_MS)
            }
        }
    }

    private suspend fun fetchStatus(id: String): Map<String, Any?>? {
        api.getRecordingStatuses(listOf(id)).getOrNull()?.firstOrNull { it["id"]?.toString() == id }?.let { return it }
        // Fallback for servers without /recordings/status: derive from the detail endpoint.
        val d = api.getRecordingDetail(id).getOrNull() ?: return null
        @Suppress("UNCHECKED_CAST")
        val report = d["report"] as? Map<String, Any?>
        return mapOf(
            "id" to id,
            "status" to d["status"],
            "error_message" to d["error_message"],
            "title" to d["title"],
            "report_status" to (d["report_status"] ?: report?.get("status"))
        )
    }

    /** Retry: re-transcribe, or regenerate only the report when just that failed. */
    fun retry(title: String?, onError: (String) -> Unit) {
        val id = watchedId ?: return
        val ui = transcriptionUiFrom(statusItem)
        retrying = true
        viewModelScope.launch {
            val res = if (ui is TranscriptionUi.Failed && ui.reportOnly) api.regenerateReport(id).map { true } else api.reTranscribe(id)
            retrying = false
            res.onSuccess {
                // Background watcher posts "Report ready" / "Transcription failed" (contract §8.5)
                ReportStatusWorker.enqueue(getApplication(), id, statusItem?.get("title")?.toString() ?: title, replace = true)
                watchedId = null
                watch(id)
            }.onFailure { onError(it.message ?: "Retry failed") }
        }
    }
}

private const val STATUS_POLL_MS = 3_000L
private const val RECENT_COUNT = 3
