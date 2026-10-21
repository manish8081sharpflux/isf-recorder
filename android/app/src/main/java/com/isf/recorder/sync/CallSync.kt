package com.isf.recorder.sync

import android.Manifest
import android.content.ContentUris
import android.content.Context
import android.content.pm.PackageManager
import android.os.Build
import android.provider.MediaStore
import android.util.Log
import androidx.core.content.ContextCompat
import androidx.work.*
import com.isf.recorder.data.api.ISFApiClient
import com.isf.recorder.data.prefs.UserPreferences
import java.util.concurrent.TimeUnit

/**
 * Calls (14 Oct): Jomi does not record calls. The phone's own call recorder does; this finds those files
 * (Android's media index, call-recorder folders only, newer than the moment sync was turned on) and uploads
 * each one once to /calls/upload, into the signed-in person's account. Phone files are only read.
 */
object CallSync {
    private const val TAG = "CallSync"
    private const val PERIODIC = "call_sync_periodic"
    private const val NOW = "call_sync_now"

    data class Found(val id: Long, val name: String, val size: Long, val modifiedMs: Long, val folder: String) {
        val key: String get() = "$id:$size:$modifiedMs"
    }

    fun permission(): String =
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) Manifest.permission.READ_MEDIA_AUDIO
        else Manifest.permission.READ_EXTERNAL_STORAGE

    fun hasPermission(context: Context) =
        ContextCompat.checkSelfPermission(context, permission()) == PackageManager.PERMISSION_GRANTED

    /** Call recordings on the phone, newest first (only files modified after [sinceMs]). */
    fun scan(context: Context, sinceMs: Long): List<Found> {
        if (!hasPermission(context)) return emptyList()
        val uri = MediaStore.Audio.Media.EXTERNAL_CONTENT_URI
        val folderCol = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) MediaStore.Audio.Media.RELATIVE_PATH
                        else @Suppress("DEPRECATION") MediaStore.Audio.Media.DATA
        val cols = arrayOf(MediaStore.Audio.Media._ID, MediaStore.Audio.Media.DISPLAY_NAME, MediaStore.Audio.Media.SIZE,
            MediaStore.Audio.Media.DATE_MODIFIED, folderCol)
        val out = mutableListOf<Found>()
        try {
            context.contentResolver.query(uri, cols, "${MediaStore.Audio.Media.DATE_MODIFIED} >= ?",
                arrayOf((sinceMs / 1000).toString()), "${MediaStore.Audio.Media.DATE_MODIFIED} DESC")?.use { c ->
                while (c.moveToNext()) {
                    val folder = c.getString(4) ?: ""
                    if (!CallFileParser.isCallFolder(folder)) continue
                    val size = c.getLong(2)
                    if (size < 1000) continue
                    out += Found(c.getLong(0), c.getString(1) ?: "call.m4a", size, c.getLong(3) * 1000, folder)
                }
            }
        } catch (e: Exception) {
            Log.w(TAG, "Media scan failed: ${e.message}")
        }
        return out
    }

    /** Every 15 minutes while sync is on (needs internet); also [syncNow] from the app. */
    fun schedule(context: Context) {
        val prefs = UserPreferences(context)
        val wm = WorkManager.getInstance(context.applicationContext)
        if (!prefs.callSyncOn) {
            wm.cancelUniqueWork(PERIODIC)
            return
        }
        val req = PeriodicWorkRequestBuilder<CallSyncWorker>(15, TimeUnit.MINUTES)
            .setConstraints(Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build())
            .build()
        wm.enqueueUniquePeriodicWork(PERIODIC, ExistingPeriodicWorkPolicy.KEEP, req)
    }

    fun syncNow(context: Context) {
        val req = OneTimeWorkRequestBuilder<CallSyncWorker>()
            .setConstraints(Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build())
            .build()
        WorkManager.getInstance(context.applicationContext).enqueueUniqueWork(NOW, ExistingWorkPolicy.KEEP, req)
    }

    /** One pass: upload the call files not uploaded yet. Returns how many were sent. */
    suspend fun runOnce(context: Context): Int {
        val prefs = UserPreferences(context)
        if (!prefs.callSyncOn || prefs.token.isNullOrBlank() || !hasPermission(context)) return 0
        val done = prefs.syncedCallKeys()
        val api = ISFApiClient(context)
        var sent = 0
        for (f in scan(context, prefs.callSyncSinceMs).sortedBy { it.modifiedMs }) {
            if (f.key in done) continue
            val parsed = CallFileParser.parse(f.name)
            val app = when {
                f.folder.contains("miui", true) || f.folder.contains("sound_recorder", true) -> "xiaomi"
                f.folder.contains("acr", true) -> "cube_acr"
                else -> "phone"
            }
            val res = api.uploadCall(
                uri = ContentUris.withAppendedId(MediaStore.Audio.Media.EXTERNAL_CONTENT_URI, f.id),
                fileName = f.name, sizeBytes = f.size, contactName = parsed.contactName, contactPhone = parsed.contactPhone,
                callTimeMs = parsed.callTimeMs ?: f.modifiedMs, callApp = app, fileKey = "${f.name}|${f.size}|${f.modifiedMs}"
            )
            if (res.isSuccess) {
                prefs.addSyncedCallKey(f.key)
                sent++
            } else {
                Log.w(TAG, "Upload of ${f.name} failed: ${res.exceptionOrNull()?.message}")
                break       // try again on the next run (offline / server busy)
            }
        }
        prefs.lastCallSyncMs = System.currentTimeMillis()
        return sent
    }
}

class CallSyncWorker(context: Context, params: WorkerParameters) : CoroutineWorker(context, params) {
    override suspend fun doWork(): Result = try {
        CallSync.runOnce(applicationContext)
        Result.success()
    } catch (e: Exception) {
        Log.w("CallSyncWorker", "Call sync failed: ${e.message}")
        Result.retry()
    }
}
