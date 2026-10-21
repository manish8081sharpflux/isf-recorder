package com.isf.recorder.ui.components

import android.content.Context
import android.content.Intent
import android.net.Uri
import android.widget.Toast
import androidx.compose.material3.*
import androidx.compose.runtime.Composable
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.window.DialogProperties
import com.isf.recorder.BuildConfig
import com.isf.recorder.data.api.ISFApiClient
import com.isf.recorder.data.prefs.UserPreferences
import com.isf.recorder.service.AudioRecordingService
import com.isf.recorder.ui.theme.JomiGreen

data class AppUpdate(val versionCode: Int, val versionName: String, val notes: String, val apkUrl: String, val force: Boolean)

object UpdateCheck {
    private const val MIN_INTERVAL_MS = 6 * 60 * 60 * 1000L

    /** Pure decision (unit-tested): null = no prompt. */
    fun decide(latest: Map<String, Any?>, myCode: Int, applicationId: String): AppUpdate? {
        val code = (latest["version_code"] as? Number)?.toInt() ?: return null
        val min = (latest["min_supported_version_code"] as? Number)?.toInt() ?: 1
        val url = latest["apk_url"]?.toString()?.takeIf { it.startsWith("http") } ?: return null
        // TEST builds (.test) only get the prompt when their (test) server says so explicitly
        if (applicationId.endsWith(".test") && latest["notify_test_builds"] != true) return null
        if (code <= myCode) return null
        return AppUpdate(code, latest["version_name"]?.toString().orEmpty(), latest["notes"]?.toString().orEmpty(),
            url, force = myCode < min)
    }

    /** At most once per 6 h, never while a recording session exists. */
    suspend fun check(api: ISFApiClient, prefs: UserPreferences, now: Long = System.currentTimeMillis()): AppUpdate? {
        if (AudioRecordingService.sessionState.value != null) return null
        if (now - prefs.lastUpdateCheckMs < MIN_INTERVAL_MS) return null
        val latest = api.getLatestApp().getOrNull() ?: return null
        prefs.lastUpdateCheckMs = now
        return decide(latest, BuildConfig.VERSION_CODE, BuildConfig.APPLICATION_ID)
    }

    fun openDownload(context: Context, url: String) {
        try {
            context.startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(url)).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
        } catch (e: Exception) {
            Toast.makeText(context, "Open $url in your browser to update", Toast.LENGTH_LONG).show()
        }
    }
}

@Composable
fun UpdateDialog(update: AppUpdate, onUpdate: () -> Unit, onLater: () -> Unit) {
    AlertDialog(
        onDismissRequest = { if (!update.force) onLater() },
        properties = DialogProperties(dismissOnBackPress = !update.force, dismissOnClickOutside = !update.force),
        title = { Text("Update available — Jomi ${update.versionName}".trim(), fontWeight = FontWeight.Bold) },
        text = {
            Text(buildString {
                if (update.notes.isNotBlank()) append(update.notes.trim()).append("\n\n")
                append(if (update.force) "This version of the app is no longer supported. Please update to continue."
                       else "Download and install the new version.")
            })
        },
        confirmButton = {
            Button(onClick = onUpdate, colors = ButtonDefaults.buttonColors(containerColor = JomiGreen)) { Text("Update now") }
        },
        dismissButton = if (update.force) null else ({ TextButton(onClick = onLater) { Text("Later") } })
    )
}
