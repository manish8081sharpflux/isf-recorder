package com.isf.recorder.ui.components

import android.app.NotificationManager
import android.content.Context
import android.content.Intent
import android.os.Build
import android.provider.Settings
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.core.app.NotificationManagerCompat
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import androidx.compose.ui.platform.LocalLifecycleOwner
import com.isf.recorder.service.RecorderNotifications
import com.isf.recorder.ui.theme.*

/** True when the app may show its alerts (app notifications on and the "Recording alerts" channel not blocked). */
fun alertsAllowed(context: Context): Boolean {
    if (!NotificationManagerCompat.from(context).areNotificationsEnabled()) return false
    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
        val ch = context.getSystemService(NotificationManager::class.java)
            ?.getNotificationChannel(RecorderNotifications.CHANNEL_ALERTS)
        if (ch != null && ch.importance == NotificationManager.IMPORTANCE_NONE) return false
    }
    return true
}

/**
 * Shown on Home when notifications are switched off: without them the coach never sees "Report ready",
 * "Recording paused" or mic alerts. Re-checked every time the app comes back to the front.
 */
@Composable
fun NotificationsOffBanner(modifier: Modifier = Modifier) {
    val context = LocalContext.current
    val owner = LocalLifecycleOwner.current
    var allowed by remember { mutableStateOf(alertsAllowed(context)) }
    DisposableEffect(owner) {
        val obs = LifecycleEventObserver { _, e -> if (e == Lifecycle.Event.ON_RESUME) allowed = alertsAllowed(context) }
        owner.lifecycle.addObserver(obs)
        onDispose { owner.lifecycle.removeObserver(obs) }
    }
    if (allowed) return
    Surface(
        color = RecordRed.copy(alpha = 0.08f),
        shape = RoundedCornerShape(14.dp),
        border = BorderStroke(1.5.dp, RecordRed),
        modifier = modifier.fillMaxWidth()
    ) {
        Row(Modifier.padding(12.dp), verticalAlignment = Alignment.CenterVertically) {
            Column(Modifier.weight(1f)) {
                Text("🔕 Notifications are off", color = RecordRed, fontWeight = FontWeight.Bold, fontSize = 14.sp)
                Text("You will not get mic alerts or \"Report ready\".", fontSize = 12.sp, color = JomiTextMuted)
            }
            Button(
                onClick = {
                    val i = Intent(Settings.ACTION_APP_NOTIFICATION_SETTINGS)
                        .putExtra(Settings.EXTRA_APP_PACKAGE, context.packageName)
                        .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                    try { context.startActivity(i) } catch (_: Exception) {
                        context.startActivity(Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS,
                            android.net.Uri.fromParts("package", context.packageName, null)).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
                    }
                },
                colors = ButtonDefaults.buttonColors(containerColor = RecordRed),
                contentPadding = PaddingValues(horizontal = 12.dp, vertical = 6.dp)
            ) { Text("Turn on", fontSize = 13.sp) }
        }
    }
}
