package com.isf.recorder

import android.app.Application
import android.util.Log
import com.isf.recorder.service.OrphanRecovery
import com.isf.recorder.service.RecorderNotifications
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch

class ISFApplication : Application() {
    companion object {
        lateinit var instance: ISFApplication
            private set
        val applicationScope = CoroutineScope(SupervisorJob() + Dispatchers.Main)

        /** Wall-clock time this process started; sessions started earlier and still ACTIVE are orphans. */
        @Volatile var processStartMs: Long = 0L
            private set
    }

    override fun onCreate() {
        super.onCreate()
        instance = this
        processStartMs = System.currentTimeMillis()
        // The only place notification channels are created.
        RecorderNotifications.ensureChannels(this)
        // Calls (14 Oct): keep the 15-minute call-recording sync running while it is switched on
        com.isf.recorder.sync.CallSync.schedule(this)
        // Bug M: a recording whose process was killed is saved and finished (nothing in this process records yet).
        val cutoff = processStartMs
        applicationScope.launch(Dispatchers.IO) {
            try {
                OrphanRecovery.run(this@ISFApplication, cutoffMs = cutoff)
            } catch (t: Throwable) {
                Log.e("ISFApplication", "Orphan recovery failed: ${t.message}", t)
            }
        }
    }
}
