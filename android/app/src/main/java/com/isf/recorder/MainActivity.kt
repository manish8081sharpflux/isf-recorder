package com.isf.recorder

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.widget.Toast
import androidx.activity.ComponentActivity
import androidx.activity.compose.BackHandler
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.animation.*
import androidx.compose.animation.core.tween
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.systemBarsPadding
import androidx.compose.material3.Surface
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.saveable.rememberSaveableStateHolder
import androidx.compose.ui.Modifier
import androidx.core.content.ContextCompat
import androidx.lifecycle.viewmodel.compose.viewModel
import com.isf.recorder.data.api.ISFApiClient
import com.isf.recorder.data.prefs.UserPreferences
import com.isf.recorder.service.RecorderNotifications
import com.isf.recorder.ui.components.AppUpdate
import com.isf.recorder.ui.components.UpdateCheck
import com.isf.recorder.ui.components.UpdateDialog
import com.isf.recorder.ui.screens.*
import com.isf.recorder.ui.theme.ISFRecorderTheme

sealed class Screen {
    object Login : Screen()
    object Home : Screen()
    /** "See all" — whole org for admins, own recordings for coaches. */
    object AllRecordings : Screen()
    /** Transcript (player, speaker turns, search; admins: versions + try another model). */
    data class Transcript(val recordingId: String) : Screen()
    data class Report(val recordingId: String) : Screen()
    object AskJomi : Screen()
    object Settings : Screen()
    /**
     * A chat. [conversationId] opens a saved conversation; without it a "session" chat opens the
     * recording's latest conversation (or starts one). [newChat] starts a fresh conversation.
     */
    data class Chat(
        val mode: String,
        val recordingId: String? = null,
        val recordingTitle: String? = null,
        val initialPrompt: String? = null,
        val conversationId: String? = null,
        val newChat: Boolean = false
    ) : Screen()

    /** Stable text form (21 Oct): the open screens are saved with it, so after Android closes Jomi in the
     *  background, reopening rebuilds the same screens. Also the key of each screen's saved state. */
    fun encode(): String = com.google.gson.Gson().toJson(
        when (this) {
            Login -> mapOf("s" to "login")
            Home -> mapOf("s" to "home")
            AllRecordings -> mapOf("s" to "all")
            AskJomi -> mapOf("s" to "ask")
            Settings -> mapOf("s" to "settings")
            is Transcript -> mapOf("s" to "transcript", "id" to recordingId)
            is Report -> mapOf("s" to "report", "id" to recordingId)
            is Chat -> mapOf("s" to "chat", "mode" to mode, "id" to recordingId, "title" to recordingTitle,
                "prompt" to initialPrompt, "conv" to conversationId, "new" to newChat)
        }
    )

    companion object {
        fun decode(text: String): Screen? = runCatching {
            @Suppress("UNCHECKED_CAST")
            val m = com.google.gson.Gson().fromJson(text, Map::class.java) as Map<String, Any?>
            fun s(k: String) = m[k]?.toString()
            when (s("s")) {
                "login" -> Login
                "home" -> Home
                "all" -> AllRecordings
                "ask" -> AskJomi
                "settings" -> Settings
                "transcript" -> Transcript(s("id")!!)
                "report" -> Report(s("id")!!)
                "chat" -> Chat(s("mode") ?: "all_sessions", s("id"), s("title"), s("prompt"), s("conv"), m["new"] == true)
                else -> null
            }
        }.getOrNull()

        /** Saves the back stack as text; a list that cannot be read back starts at [fallback]. */
        fun stackSaver(fallback: () -> Screen) = androidx.compose.runtime.saveable.listSaver<
            androidx.compose.runtime.snapshots.SnapshotStateList<Screen>, String>(
            save = { list -> list.map { it.encode() } },
            restore = { saved ->
                val screens = saved.mapNotNull { decode(it) }.ifEmpty { listOf(fallback()) }
                mutableStateListOf(*screens.toTypedArray())
            }
        )
    }
}

class MainActivity : ComponentActivity() {

    companion object {
        /** Set by the "Report ready" notification (sync/ReportStatusWorker, contract §8.5). */
        const val EXTRA_OPEN_RECORDING_ID = RecorderNotifications.EXTRA_OPEN_RECORDING_ID
    }

    private val requestPermissionLauncher = registerForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions()
    ) { _ -> }

    /** Pending deep link from a notification tap; consumed by the composition. */
    private val pendingOpenRecordingId = mutableStateOf<String?>(null)
    /** Bumped on every onResume: the update check runs on start and on return to the foreground. */
    private val resumeCount = mutableIntStateOf(0)

    override fun onResume() {
        super.onResume()
        resumeCount.intValue++
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        requestRequiredPermissions()
        handleIntent(intent)

        val prefs = UserPreferences(this)
        val api = ISFApiClient(this)

        setContent {
            ISFRecorderTheme {
                Surface(
                    modifier = Modifier
                        .fillMaxSize()
                        .systemBarsPadding(),
                    color = androidx.compose.material3.MaterialTheme.colorScheme.background
                ) {
                    // rememberSaveable: if Android closes Jomi in the background, reopening shows the same screens
                    val startScreen = { if (prefs.isLoggedIn) Screen.Home else Screen.Login }
                    val backStack = rememberSaveable(saver = Screen.stackSaver(startScreen)) {
                        mutableStateListOf(startScreen())
                    }
                    // a session that ended while the app was closed must not reopen inside the app
                    LaunchedEffect(Unit) {
                        if (!prefs.isLoggedIn && backStack.lastOrNull() != Screen.Login) { backStack.clear(); backStack.add(Screen.Login) }
                    }
                    // Bumped when a super admin switches "View as Coach" so every screen re-reads the role.
                    var roleEpoch by remember { mutableIntStateOf(0) }
                    val homeVm: HomeViewModel = viewModel()

                    val currentScreen = backStack.lastOrNull() ?: if (prefs.isLoggedIn) Screen.Home else Screen.Login

                    // Each screen in the back stack keeps its state (scroll, open card, selection) while another
                    // screen is on top, so Back returns to exactly where the person was. Closed screens forget it.
                    val screenStates = rememberSaveableStateHolder()

                    fun navigateTo(screen: Screen) {
                        // Settings is a fresh start: Home shows "Previous recordings" again afterwards
                        if (screen == Screen.Settings) homeVm.allExpanded = false
                        backStack.add(screen)
                    }

                    fun navigateBack() {
                        if (backStack.size > 1) {
                            val closed = backStack.removeAt(backStack.size - 1)
                            if (closed !in backStack) screenStates.removeState(closed.encode())
                        }
                    }

                    fun resetTo(screen: Screen) {
                        backStack.forEach { screenStates.removeState(it.encode()) }
                        backStack.clear()
                        backStack.add(screen)
                    }

                    fun signOut() {
                        prefs.clear()
                        homeVm.resetSelections()
                        resetTo(Screen.Login)
                    }

                    // Archived / removed users (401) are signed out (contract §2.1).
                    LaunchedEffect(Unit) {
                        ISFApiClient.sessionExpired.collect { reason ->
                            Toast.makeText(this@MainActivity, reason, Toast.LENGTH_LONG).show()
                            homeVm.resetSelections()
                            resetTo(Screen.Login)
                        }
                    }
                    // First login (or an existing placeholder "Staff 1234" user): ask for the name.
                    var askName by remember { mutableStateOf(prefs.isLoggedIn && prefs.needsName) }
                    // Refresh role / super-admin flag once per launch.
                    LaunchedEffect(Unit) {
                        if (prefs.isLoggedIn) api.getMe().onSuccess { askName = prefs.needsName }
                    }
                    // "Update available" (21 Oct): on every app open and every 15 min while it stays open, never
                    // during a recording. "Later" hides it until the next time the app is opened.
                    var update by remember { mutableStateOf<AppUpdate?>(null) }
                    var laterFor by remember { mutableIntStateOf(0) }
                    val resumes = resumeCount.intValue
                    LaunchedEffect(resumes) {
                        if (prefs.isLoggedIn) com.isf.recorder.sync.PhoneNotifications.collectNow(this@MainActivity)
                        laterFor = 0
                        while (prefs.isLoggedIn) {
                            if (lifecycle.currentState.isAtLeast(androidx.lifecycle.Lifecycle.State.RESUMED)) {
                                val found = UpdateCheck.check(api, prefs)
                                if (found != null && found.versionCode != laterFor) {
                                    update = found
                                    // Seen in the app: no phone notification needed for this version any more
                                    if (prefs.lastNotifiedUpdateCode < found.versionCode) prefs.lastNotifiedUpdateCode = found.versionCode
                                    com.isf.recorder.sync.UpdateNotifier.clear(this@MainActivity)
                                }
                            }
                            kotlinx.coroutines.delay(UpdateCheck.OPEN_APP_INTERVAL_MS)
                        }
                    }
                    update?.let { u ->
                        UpdateDialog(u, onUpdate = { UpdateCheck.openDownload(this@MainActivity, u.apkUrl) },
                            onLater = { laterFor = u.versionCode; update = null })
                    }
                    // Notification deep link → Report screen.
                    val openId = pendingOpenRecordingId.value
                    LaunchedEffect(openId) {
                        if (openId != null) {
                            pendingOpenRecordingId.value = null
                            if (prefs.isLoggedIn) {
                                if (prefs.isAdmin) {
                                    if (backStack.lastOrNull() != Screen.Report(openId)) backStack.add(Screen.Report(openId))
                                } else {
                                    // Coaches stay on the one-page Home: player + report popup.
                                    homeVm.pendingReportId = openId
                                    resetTo(Screen.Home)
                                }
                            }
                        }
                    }

                    BackHandler(enabled = backStack.size > 1) { navigateBack() }
                    // On the first screen: one Back only warns, a second Back within 2 s closes Jomi
                    // (a recording keeps running in its own service either way). Screens' own Back handlers win.
                    var exitArmedAt by remember { mutableLongStateOf(0L) }
                    BackHandler(enabled = backStack.size <= 1) {
                        val now = System.currentTimeMillis()
                        if (now - exitArmedAt < 2_000) finish()
                        else {
                            exitArmedAt = now
                            Toast.makeText(this@MainActivity, "Press Back again to exit", Toast.LENGTH_SHORT).show()
                        }
                    }

                    if (askName && prefs.isLoggedIn && currentScreen !is Screen.Login) {
                        AskNameScreen(api = api, onDone = { askName = false; homeVm.refresh() })
                    } else key(roleEpoch) {
                        AnimatedContent(
                            targetState = currentScreen,
                            transitionSpec = {
                                if (targetState is Screen.Home && initialState !is Screen.Home) {
                                    (slideInHorizontally(animationSpec = tween(220)) { -it } + fadeIn())
                                        .togetherWith(slideOutHorizontally(animationSpec = tween(220)) { it } + fadeOut())
                                } else {
                                    (slideInHorizontally(animationSpec = tween(220)) { it } + fadeIn())
                                        .togetherWith(slideOutHorizontally(animationSpec = tween(220)) { -it / 3 } + fadeOut())
                                }
                            },
                            label = "ScreenTransition"
                        ) { target ->
                          screenStates.SaveableStateProvider(target.encode()) {
                            when (target) {
                                is Screen.Login -> LoginScreen(onLoginSuccess = { askName = prefs.needsName; resetTo(Screen.Home) })

                                is Screen.Home -> HomeScreen(
                                    onSeeAll = { navigateTo(Screen.AllRecordings) },
                                    onOpenChat = { id, title -> navigateTo(Screen.Chat(mode = "session", recordingId = id, recordingTitle = title)) },
                                    onSettings = { navigateTo(Screen.Settings) },
                                    onOpenFullReport = { navigateTo(Screen.Report(it)) },
                                    onOpenTranscript = { navigateTo(Screen.Transcript(it)) },
                                    vm = homeVm
                                )

                                is Screen.AllRecordings -> RecordingsListScreen(
                                    onSelectRecording = { navigateTo(Screen.Report(it)) },
                                    onOpenReport = { navigateTo(Screen.Report(it)) },
                                    onOpenChat = { id, title -> navigateTo(Screen.Chat(mode = "session", recordingId = id, recordingTitle = title)) },
                                    onBack = { navigateBack() }
                                )

                                is Screen.Transcript -> TranscriptScreen(recordingId = target.recordingId, onBack = { navigateBack() })

                                is Screen.Report -> ReportScreen(
                                    recordingId = target.recordingId,
                                    onBack = { navigateBack() },
                                    onViewTranscript = { id -> navigateTo(Screen.Transcript(id)) },
                                    onOpenChat = { id, title -> navigateTo(Screen.Chat(mode = "session", recordingId = id, recordingTitle = title)) }
                                )

                                is Screen.AskJomi -> AskJomiScreen(
                                    onBack = { navigateBack() },
                                    onOpenConversation = { conv ->
                                        val recId = conv["recording_id"]?.toString()?.takeIf { it.isNotBlank() && it != "null" }
                                        navigateTo(
                                            Screen.Chat(
                                                mode = conv["mode"]?.toString()?.takeIf { it == "session" || it == "all_sessions" }
                                                    ?: if (recId != null) "session" else "all_sessions",
                                                recordingId = recId,
                                                recordingTitle = conv["recording_title"]?.toString(),
                                                conversationId = conv["id"]?.toString()
                                            )
                                        )
                                    },
                                    onNewChat = { navigateTo(Screen.Chat(mode = "all_sessions", newChat = true)) }
                                )

                                is Screen.Chat -> SessionChatScreen(
                                    mode = target.mode,
                                    recordingId = target.recordingId,
                                    recordingTitle = target.recordingTitle,
                                    initialPrompt = target.initialPrompt,
                                    conversationId = target.conversationId,
                                    newChat = target.newChat,
                                    onBack = { navigateBack() }
                                )

                                is Screen.Settings -> SettingsScreen(
                                    onLogout = { signOut() },
                                    onBack = { navigateBack() },
                                    onAskJomi = { navigateTo(Screen.AskJomi) },
                                    onSwitchView = {
                                        prefs.viewAsCoach = !prefs.viewAsCoach
                                        resetTo(Screen.Home)
                                        roleEpoch++
                                    }
                                )
                            }
                          }
                        }
                    }
                }
            }
        }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        handleIntent(intent)
    }

    private fun handleIntent(intent: Intent?) {
        val id = intent?.getStringExtra(EXTRA_OPEN_RECORDING_ID)?.takeIf { it.isNotBlank() } ?: return
        intent.removeExtra(EXTRA_OPEN_RECORDING_ID)
        pendingOpenRecordingId.value = id
    }

    private fun requestRequiredPermissions() {
        val permissions = mutableListOf(Manifest.permission.RECORD_AUDIO)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            permissions.add(Manifest.permission.POST_NOTIFICATIONS)
        }
        val missing = permissions.filter {
            ContextCompat.checkSelfPermission(this, it) != PackageManager.PERMISSION_GRANTED
        }
        if (missing.isNotEmpty()) requestPermissionLauncher.launch(missing.toTypedArray())
    }
}
