package com.isf.recorder.ui.screens

import android.widget.Toast
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.isf.recorder.data.api.ISFApiClient
import com.isf.recorder.data.prefs.UserPreferences
import com.isf.recorder.sync.CallSync
import com.isf.recorder.sync.TaskReminder
import com.isf.recorder.ui.theme.*
import kotlinx.coroutines.launch
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * Settings → 📞 Calls (14 Oct): sync the phone's own call recordings (only calls recorded after it is turned
 * on), and the daily task reminder. Everyone has it; calls stay private to the person.
 */
@Composable
fun CallsSettingsSection(api: ISFApiClient) {
    val context = LocalContext.current
    val prefs = remember { UserPreferences(context) }
    val scope = rememberCoroutineScope()
    var syncOn by remember { mutableStateOf(prefs.callSyncOn) }
    var allowed by remember { mutableStateOf(CallSync.hasPermission(context)) }
    var lastSync by remember { mutableStateOf(prefs.lastCallSyncMs) }
    var remindOn by remember { mutableStateOf(true) }
    var remindTime by remember { mutableStateOf("09:00") }
    var openCount by remember { mutableStateOf(0) }

    fun turnOn() {
        prefs.callSyncOn = true
        if (prefs.callSyncSinceMs == 0L) prefs.callSyncSinceMs = System.currentTimeMillis()
        syncOn = true
        CallSync.schedule(context)
        CallSync.syncNow(context)
    }
    val permission = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { ok ->
        allowed = ok
        if (ok) turnOn() else Toast.makeText(context, "Without this permission Jomi cannot find your call recordings", Toast.LENGTH_LONG).show()
    }
    LaunchedEffect(Unit) {
        api.getTaskReminder().onSuccess { r ->
            remindOn = r["on"] as? Boolean ?: true
            remindTime = r["time"]?.toString() ?: "09:00"
            openCount = (r["open_count"] as? Number)?.toInt() ?: 0
        }
    }

    Column {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Column(Modifier.weight(1f)) {
                Text("Sync my call recordings", fontWeight = FontWeight.SemiBold, fontSize = 15.sp)
                Text("Calls recorded by your phone's own call recorder appear in Calls with a summary and tasks. " +
                    "Only you can see them. Only calls recorded after you turn this on.", color = JomiTextMuted, fontSize = 12.sp)
            }
            Switch(checked = syncOn, onCheckedChange = { on ->
                if (on) {
                    if (CallSync.hasPermission(context)) turnOn() else permission.launch(CallSync.permission())
                } else {
                    prefs.callSyncOn = false
                    syncOn = false
                    CallSync.schedule(context)
                }
            }, colors = SwitchDefaults.colors(checkedTrackColor = JomiGreen))
        }
        if (syncOn) {
            Spacer(Modifier.height(6.dp))
            Text(
                if (!allowed) "⚠️ Permission to read audio files is off — tap the switch again"
                else if (lastSync > 0) "Last checked: " + SimpleDateFormat("d MMM, h:mm a", Locale.ENGLISH).format(Date(lastSync))
                else "Not checked yet",
                color = if (!allowed) RecordRed else JomiTextMuted, fontSize = 12.sp
            )
            TextButton(onClick = {
                CallSync.syncNow(context)
                Toast.makeText(context, "Checking for new calls…", Toast.LENGTH_SHORT).show()
                lastSync = System.currentTimeMillis()
            }) { Text("🔄 Sync now", color = JomiTeal) }
            Text("Make sure your phone's call recording is switched on (phone app → Settings → Record calls). " +
                "WhatsApp calls cannot be recorded on Android.", color = JomiTextMuted, fontSize = 11.sp)
        }

        HorizontalDivider(Modifier.padding(vertical = 12.dp), color = JomiBorder)
        Row(verticalAlignment = Alignment.CenterVertically) {
            Column(Modifier.weight(1f)) {
                Text("Daily task reminder", fontWeight = FontWeight.SemiBold, fontSize = 15.sp)
                Text(if (openCount > 0) "$openCount open task${if (openCount == 1) "" else "s"} now" else "A notification when tasks are open",
                    color = JomiTextMuted, fontSize = 12.sp)
            }
            Switch(checked = remindOn, onCheckedChange = { on ->
                remindOn = on
                scope.launch {
                    api.setTaskReminder(on = on).onSuccess { TaskReminder.schedule(context, on, remindTime) }
                        .onFailure { Toast.makeText(context, it.message ?: "Could not save", Toast.LENGTH_SHORT).show() }
                }
            }, colors = SwitchDefaults.colors(checkedTrackColor = JomiGreen))
        }
        if (remindOn) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text("Time:", fontSize = 14.sp)
                Spacer(Modifier.width(8.dp))
                listOf("08:00", "09:00", "12:00", "18:00", "20:00").forEach { t ->
                    FilterChip(selected = remindTime == t, onClick = {
                        remindTime = t
                        scope.launch {
                            api.setTaskReminder(time = t).onSuccess { TaskReminder.schedule(context, true, t) }
                        }
                    }, label = { Text(t, fontSize = 12.sp) }, modifier = Modifier.padding(end = 4.dp))
                }
            }
        }
    }
}
