package com.isf.recorder.sync

import android.content.Context
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import android.net.NetworkRequest
import android.util.Log
import androidx.work.Constraints
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.WorkManager
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow

class NetworkMonitor private constructor(private val appContext: Context) {
    private val connectivityManager = appContext.getSystemService(Context.CONNECTIVITY_SERVICE) as ConnectivityManager

    private val _isOnline = MutableStateFlow(checkIsOnline())
    val isOnline: StateFlow<Boolean> = _isOnline.asStateFlow()

    init {
        try {
            val request = NetworkRequest.Builder()
                .addCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET)
                .build()

            connectivityManager.registerNetworkCallback(request, object : ConnectivityManager.NetworkCallback() {
                override fun onAvailable(network: Network) {
                    _isOnline.value = true
                    try {
                        // Safely trigger offline sync without instantiating ChunkUploader (avoids recursive callbacks)
                        val constraints = Constraints.Builder()
                            .setRequiredNetworkType(NetworkType.CONNECTED)
                            .build()
                        val syncRequest = OneTimeWorkRequestBuilder<OfflineSyncWorker>()
                            .setConstraints(constraints)
                            .build()
                        WorkManager.getInstance(appContext).enqueueUniqueWork(
                            "ISF_CHUNK_OFFLINE_SYNC",
                            ExistingWorkPolicy.KEEP,
                            syncRequest
                        )
                    } catch (e: Exception) {
                        Log.w("NetworkMonitor", "Could not enqueue sync on network available: ${e.message}")
                    }
                }

                override fun onLost(network: Network) {
                    _isOnline.value = checkIsOnline()
                }
            })
        } catch (e: Exception) {
            Log.e("NetworkMonitor", "Failed to register network callback: ${e.message}")
        }
    }

    fun isCurrentlyOnline(): Boolean = checkIsOnline()

    private fun checkIsOnline(): Boolean {
        return try {
            val activeNetwork = connectivityManager.activeNetwork ?: return false
            val caps = connectivityManager.getNetworkCapabilities(activeNetwork) ?: return false
            caps.hasCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET)
        } catch (e: Exception) {
            true // Assume online if check fails
        }
    }

    companion object {
        @Volatile
        private var INSTANCE: NetworkMonitor? = null

        fun getInstance(context: Context): NetworkMonitor {
            return INSTANCE ?: synchronized(this) {
                INSTANCE ?: NetworkMonitor(context.applicationContext).also { INSTANCE = it }
            }
        }
    }
}
