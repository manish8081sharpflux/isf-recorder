package com.isf.recorder.data.api

import android.content.ContentResolver
import android.content.Context
import android.net.Uri
import com.google.gson.Gson
import com.google.gson.reflect.TypeToken
import com.isf.recorder.data.prefs.UserPreferences
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.flow.asSharedFlow
import kotlinx.coroutines.withContext
import okhttp3.*
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.RequestBody.Companion.asRequestBody
import okhttp3.RequestBody.Companion.toRequestBody
import okio.BufferedSink
import java.io.File
import java.io.IOException
import java.util.concurrent.TimeUnit

class ISFApiClient(context: Context) {
    private val prefs = UserPreferences(context)
    private val appContext = context.applicationContext

    init {
        sessionContext = context.applicationContext
    }

    companion object {
        private val sharedConnectionPool = ConnectionPool(10, 5, TimeUnit.MINUTES)

        @Volatile
        private var sessionContext: Context? = null

        private val _sessionExpired = MutableSharedFlow<String>(extraBufferCapacity = 4)

        /**
         * Emits a user-facing reason when the server rejected our token with 401
         * (archived / removed user, or expired login). The session is already cleared;
         * MainActivity collects this and returns to the Login screen.
         */
        val sessionExpired: SharedFlow<String> = _sessionExpired.asSharedFlow()

        /** Clears the stored session when an authenticated call comes back 401 (contract §2.1). */
        private val unauthorizedInterceptor = Interceptor { chain ->
            val request = chain.request()
            val response = chain.proceed(request)
            val authHeader = request.header("Authorization")
            if (response.code == 401 && authHeader != null) {
                val ctx = sessionContext
                if (ctx != null) {
                    val p = UserPreferences(ctx)
                    // Only react if the rejected token is still the current one (not an old request after re-login)
                    if (!p.token.isNullOrBlank() && authHeader == "Bearer ${p.token}") {
                        val detail = try {
                            val raw = response.peekBody(2048).string()
                            (Gson().fromJson(raw, Map::class.java)?.get("detail") as? String)
                        } catch (e: Exception) {
                            null
                        }
                        p.clear()
                        _sessionExpired.tryEmit(detail?.takeIf { it.isNotBlank() } ?: "Your session has ended. Please sign in again.")
                    }
                }
            }
            response
        }

        val sharedOkHttpClient: OkHttpClient by lazy {
            OkHttpClient.Builder()
                .connectionPool(sharedConnectionPool)
                .connectTimeout(15, TimeUnit.SECONDS)
                .readTimeout(60, TimeUnit.SECONDS)
                .writeTimeout(60, TimeUnit.SECONDS)
                .retryOnConnectionFailure(true)
                .addInterceptor(unauthorizedInterceptor)
                .build()
        }

        private val sharedGson = Gson()
    }

    /** Thrown for non-2xx responses so callers can branch on the HTTP code (e.g. 404 / 409). */
    class ApiException(val code: Int, message: String, val body: Map<String, Any?>? = null) : Exception(message)

    private val okHttpClient get() = sharedOkHttpClient
    private val gson get() = sharedGson
    private val jsonMediaType = "application/json; charset=utf-8".toMediaType()

    private fun buildRequest(url: String): Request.Builder {
        val builder = Request.Builder().url(url)
        prefs.token?.let {
            builder.addHeader("Authorization", "Bearer $it")
        }
        return builder
    }

    // ── Auth Endpoints ──────────────────────────────────────────────────────────

    suspend fun requestOtp(phone: String): Result<String> = withContext(Dispatchers.IO) {
        try {
            val body = gson.toJson(mapOf("phone" to phone)).toRequestBody(jsonMediaType)
            val request = Request.Builder()
                .url("${prefs.serverUrl}/auth/request-otp")
                .post(body)
                .build()

            val response = okHttpClient.newCall(request).execute()
            val responseBody = response.body?.string() ?: ""
            if (response.isSuccessful) {
                Result.success("OTP sent to WhatsApp")
            } else {
                Result.failure(Exception("Failed: $responseBody"))
            }
        } catch (e: Exception) {
            Result.failure(e)
        }
    }

    suspend fun verifyOtp(phone: String, code: String): Result<Map<String, Any?>> = withContext(Dispatchers.IO) {
        try {
            val body = gson.toJson(mapOf("phone" to phone, "code" to code)).toRequestBody(jsonMediaType)
            val request = Request.Builder()
                .url("${prefs.serverUrl}/auth/verify-otp")
                .post(body)
                .build()

            val response = okHttpClient.newCall(request).execute()
            val responseBody = response.body?.string() ?: ""
            if (response.isSuccessful) {
                val mapType = object : TypeToken<Map<String, Any?>>() {}.type
                val data: Map<String, Any?> = gson.fromJson(responseBody, mapType)
                val token = data["token"] as? String ?: ""
                @Suppress("UNCHECKED_CAST")
                val user = data["user"] as? Map<String, Any?> ?: emptyMap()
                prefs.saveUserSession(token, user)
                Result.success(data)
            } else {
                val cleanError = try {
                    val map = gson.fromJson(responseBody, Map::class.java)
                    map["detail"] as? String ?: responseBody
                } catch (e: Exception) {
                    responseBody
                }
                Result.failure(Exception(cleanError.ifBlank { "Invalid OTP or phone number" }))
            }
        } catch (e: Exception) {
            Result.failure(e)
        }
    }

    // ── Recording Lifecycle ─────────────────────────────────────────────────────

    suspend fun startRecording(sessionId: String, micSource: String): Result<String> = withContext(Dispatchers.IO) {
        try {
            val payload = mapOf(
                "session_id" to sessionId,
                "mic_source" to micSource,
                "sample_rate" to 16000
            )
            val body = gson.toJson(payload).toRequestBody(jsonMediaType)
            val request = buildRequest("${prefs.serverUrl}/recordings/start").post(body).build()

            val response = okHttpClient.newCall(request).execute()
            val responseBody = response.body?.string() ?: ""
            if (response.isSuccessful) {
                val mapType = object : TypeToken<Map<String, Any?>>() {}.type
                val data: Map<String, Any?> = gson.fromJson(responseBody, mapType)
                Result.success(data["recording_id"]?.toString() ?: "")
            } else {
                Result.failure(Exception("Start recording failed: $responseBody"))
            }
        } catch (e: Exception) {
            Result.failure(e)
        }
    }

    suspend fun uploadChunk(
        sessionId: String,
        chunkIndex: Int,
        file: File,
        durationSeconds: Float,
        checksum: String
    ): Result<Boolean> = withContext(Dispatchers.IO) {
        try {
            val fileBody = file.asRequestBody("audio/wav".toMediaType())
            val multipartBody = MultipartBody.Builder()
                .setType(MultipartBody.FORM)
                .addFormDataPart("chunk_index", chunkIndex.toString())
                .addFormDataPart("duration_seconds", durationSeconds.toString())
                .addFormDataPart("checksum", checksum)
                .addFormDataPart("chunk_file", file.name, fileBody)
                .build()

            val request = buildRequest("${prefs.serverUrl}/recordings/$sessionId/chunk")
                .post(multipartBody)
                .build()

            val response = okHttpClient.newCall(request).execute()
            if (response.isSuccessful) {
                Result.success(true)
            } else {
                Result.failure(Exception("Chunk upload failed: ${response.code}"))
            }
        } catch (e: Exception) {
            Result.failure(e)
        }
    }

    suspend fun pauseRecording(sessionId: String, reason: String = "user"): Result<Boolean> = withContext(Dispatchers.IO) {
        try {
            val body = gson.toJson(mapOf("reason" to reason)).toRequestBody(jsonMediaType)
            val request = buildRequest("${prefs.serverUrl}/recordings/$sessionId/pause").post(body).build()
            val response = okHttpClient.newCall(request).execute()
            Result.success(response.isSuccessful)
        } catch (e: Exception) {
            Result.failure(e)
        }
    }

    suspend fun resumeRecording(sessionId: String): Result<Boolean> = withContext(Dispatchers.IO) {
        try {
            val body = "{}".toRequestBody(jsonMediaType)
            val request = buildRequest("${prefs.serverUrl}/recordings/$sessionId/resume").post(body).build()
            val response = okHttpClient.newCall(request).execute()
            Result.success(response.isSuccessful)
        } catch (e: Exception) {
            Result.failure(e)
        }
    }

    suspend fun finishRecording(sessionId: String, totalChunks: Int, durationSeconds: Int): Result<String> = withContext(Dispatchers.IO) {
        try {
            val payload = mapOf(
                "total_chunks" to totalChunks,
                "duration_seconds" to durationSeconds
            )
            val body = gson.toJson(payload).toRequestBody(jsonMediaType)
            val request = buildRequest("${prefs.serverUrl}/recordings/$sessionId/finish").post(body).build()

            val response = okHttpClient.newCall(request).execute()
            val responseBody = response.body?.string() ?: ""
            if (response.isSuccessful) {
                Result.success(responseBody)
            } else {
                Result.failure(Exception("Finish recording failed: $responseBody"))
            }
        } catch (e: Exception) {
            Result.failure(e)
        }
    }

    /** Session metadata sent with /recordings/start (contract §4.1). */
    data class StartMetadata(
        val mode: String? = null,               // "schedule" | "meeting"
        val balgruhName: String? = null,
        val assistantCoach: String? = null,
        val participantsJson: String? = null,   // JSON array [{user_id?, name}]
        val title: String? = null,
        val assistantCoachesJson: String? = null // JSON array [{user_id?, name}] → "assistant_coaches"
    )

    suspend fun startRecording(sessionId: String, micSource: String, metadata: StartMetadata): Result<String> = withContext(Dispatchers.IO) {
        try {
            val payload = mutableMapOf<String, Any?>(
                "session_id" to sessionId,
                "mic_source" to micSource,
                "sample_rate" to 16000
            )
            // app version for the admin health report ("app versions seen today")
            payload["device_info"] = mapOf(
                "app_version" to com.isf.recorder.BuildConfig.VERSION_NAME,
                "app_version_code" to com.isf.recorder.BuildConfig.VERSION_CODE,
                "model" to android.os.Build.MODEL,
                "android" to android.os.Build.VERSION.SDK_INT
            )
            metadata.mode?.let { payload["mode"] = it }
            metadata.balgruhName?.let { payload["balgruh_name"] = it }
            metadata.assistantCoach?.let { payload["assistant_coach"] = it }
            metadata.title?.takeIf { it.isNotBlank() }?.let { payload["title"] = it }
            metadata.assistantCoachesJson?.takeIf { it.isNotBlank() }?.let {
                val listType = object : TypeToken<List<Map<String, Any?>>>() {}.type
                try { payload["assistant_coaches"] = gson.fromJson<List<Map<String, Any?>>>(it, listType) } catch (_: Exception) { }
            }
            metadata.participantsJson?.let {
                val listType = object : TypeToken<List<Map<String, Any?>>>() {}.type
                payload["participants"] = gson.fromJson<List<Map<String, Any?>>>(it, listType)
            }
            val body = gson.toJson(payload).toRequestBody(jsonMediaType)
            val request = buildRequest("${prefs.serverUrl}/recordings/start").post(body).build()
            val response = okHttpClient.newCall(request).execute()
            val responseBody = response.body?.string() ?: ""
            if (response.isSuccessful) {
                val mapType = object : TypeToken<Map<String, Any?>>() {}.type
                val data: Map<String, Any?> = gson.fromJson(responseBody, mapType)
                Result.success(data["recording_id"]?.toString() ?: "")
            } else {
                Result.failure(Exception("Start recording failed: $responseBody"))
            }
        } catch (e: Exception) {
            Result.failure(e)
        }
    }

    /** Returns the server's "status": "ok" | "finalized" | "discarded" (contract §4.2). */
    suspend fun heartbeat(sessionId: String, state: String, voiceSeconds: Int, sessionSeconds: Int): Result<String> = withContext(Dispatchers.IO) {
        try {
            val payload = mapOf("state" to state, "voice_seconds" to voiceSeconds, "session_seconds" to sessionSeconds)
            val body = gson.toJson(payload).toRequestBody(jsonMediaType)
            val request = buildRequest("${prefs.serverUrl}/recordings/$sessionId/heartbeat").post(body).build()
            val response = okHttpClient.newCall(request).execute()
            val responseBody = response.body?.string() ?: ""
            if (response.isSuccessful) {
                val mapType = object : TypeToken<Map<String, Any?>>() {}.type
                val data: Map<String, Any?> = gson.fromJson(responseBody, mapType)
                Result.success(data["status"]?.toString() ?: "ok")
            } else {
                Result.failure(Exception("Heartbeat failed: ${response.code}"))
            }
        } catch (e: Exception) {
            Result.failure(e)
        }
    }

    /** Idempotent finish with voice stats (contract §4.4). [source] = "client" | "sync_worker". */
    suspend fun finishRecording(
        sessionId: String,
        totalChunks: Int,
        durationSeconds: Int,
        voiceSeconds: Int,
        silenceSkippedSeconds: Int,
        source: String = "client",
        silenceGapsJson: String? = null
    ): Result<String> = withContext(Dispatchers.IO) {
        try {
            val payload = mutableMapOf<String, Any?>(
                "total_chunks" to totalChunks,
                "duration_seconds" to durationSeconds,
                "voice_seconds" to voiceSeconds,
                "silence_skipped_seconds" to silenceSkippedSeconds
            )
            silenceGapsJson?.takeIf { it.isNotBlank() }?.let {
                val listType = object : TypeToken<List<Map<String, Any?>>>() {}.type
                try { payload["silence_gaps"] = gson.fromJson<List<Map<String, Any?>>>(it, listType).take(500) } catch (_: Exception) { }
            }
            val body = gson.toJson(payload).toRequestBody(jsonMediaType)
            val request = buildRequest("${prefs.serverUrl}/recordings/$sessionId/finish")
                .header("X-Finish-Source", source)
                .post(body)
                .build()
            val response = okHttpClient.newCall(request).execute()
            val responseBody = response.body?.string() ?: ""
            if (response.isSuccessful) {
                Result.success(responseBody)
            } else {
                Result.failure(Exception("Finish recording failed: $responseBody"))
            }
        } catch (e: Exception) {
            Result.failure(e)
        }
    }

    /** Batch status for polling (contract §4.11): items with id, status, pipeline_stage, report_status, error_message, title. */
    suspend fun getRecordingStatuses(ids: List<String>): Result<List<Map<String, Any?>>> = withContext(Dispatchers.IO) {
        try {
            if (ids.isEmpty()) return@withContext Result.success(emptyList())
            val request = buildRequest("${prefs.serverUrl}/recordings/status?ids=${ids.take(50).joinToString(",")}").get().build()
            val response = okHttpClient.newCall(request).execute()
            val responseBody = response.body?.string() ?: ""
            if (response.isSuccessful) {
                val mapType = object : TypeToken<Map<String, Any?>>() {}.type
                val data: Map<String, Any?> = gson.fromJson(responseBody, mapType)
                @Suppress("UNCHECKED_CAST")
                Result.success((data["items"] as? List<Map<String, Any?>>) ?: emptyList())
            } else {
                Result.failure(Exception("Status failed: ${response.code}"))
            }
        } catch (e: Exception) {
            Result.failure(e)
        }
    }

    // ── Queries ─────────────────────────────────────────────────────────────────

    /**
     * GET /recordings/ (contract §4.6). [dateFrom]/[dateTo] are "YYYY-MM-DD" IST calendar days (inclusive),
     * [mode] is "schedule" | "meeting".
     */
    suspend fun listRecordings(
        starredOnly: Boolean = false,
        page: Int = 1,
        limit: Int = 50,
        userId: String? = null,
        dateFrom: String? = null,
        dateTo: String? = null,
        mode: String? = null,
        balgruh: String? = null,
        assistantCoach: String? = null
    ): Result<Map<String, Any?>> = withContext(Dispatchers.IO) {
        try {
            var url = "${prefs.serverUrl}/recordings/?starred_only=$starredOnly&page=$page&limit=$limit"
            if (!userId.isNullOrBlank()) {
                url += "&user_id=$userId"
            }
            if (!dateFrom.isNullOrBlank()) url += "&date_from=$dateFrom"
            if (!dateTo.isNullOrBlank()) url += "&date_to=$dateTo"
            if (!mode.isNullOrBlank()) url += "&mode=$mode"
            if (!balgruh.isNullOrBlank()) url += "&balgruh=" + java.net.URLEncoder.encode(balgruh, "UTF-8")
            if (!assistantCoach.isNullOrBlank()) url += "&assistant_coach=" + java.net.URLEncoder.encode(assistantCoach, "UTF-8")
            val request = buildRequest(url).get().build()
            val response = okHttpClient.newCall(request).execute()
            val body = response.body?.string() ?: ""
            if (response.isSuccessful) {
                val mapType = object : TypeToken<Map<String, Any?>>() {}.type
                val data: Map<String, Any?> = gson.fromJson(body, mapType)
                Result.success(data)
            } else {
                Result.failure(Exception("List recordings failed: $body"))
            }
        } catch (e: Exception) {
            Result.failure(e)
        }
    }

    /** GET /users/ — archived users only included when [includeArchived] (admin, contract §2.4). */
    suspend fun listUsers(includeArchived: Boolean = false): Result<List<Map<String, Any?>>> = withContext(Dispatchers.IO) {
        try {
            val url = if (includeArchived) "${prefs.serverUrl}/users/?include_archived=true" else "${prefs.serverUrl}/users/"
            val request = buildRequest(url).get().build()
            val response = okHttpClient.newCall(request).execute()
            val body = response.body?.string() ?: ""
            if (response.isSuccessful) {
                val mapType = object : TypeToken<Map<String, Any?>>() {}.type
                val data: Map<String, Any?> = gson.fromJson(body, mapType)
                @Suppress("UNCHECKED_CAST")
                val users = data["users"] as? List<Map<String, Any?>> ?: emptyList()
                Result.success(users)
            } else {
                Result.failure(Exception("Failed to list users: $body"))
            }
        } catch (e: Exception) {
            Result.failure(e)
        }
    }

    suspend fun createUser(name: String, phone: String, role: String = "staff"): Result<Map<String, Any?>> = withContext(Dispatchers.IO) {
        try {
            val payload = mapOf(
                "name" to name.trim(),
                "phone" to phone.trim(),
                "role" to role
            )
            val body = gson.toJson(payload).toRequestBody(jsonMediaType)
            val request = buildRequest("${prefs.serverUrl}/users/").post(body).build()
            val response = okHttpClient.newCall(request).execute()
            val respBody = response.body?.string() ?: ""
            if (response.isSuccessful) {
                val mapType = object : TypeToken<Map<String, Any?>>() {}.type
                val data: Map<String, Any?> = gson.fromJson(respBody, mapType)
                Result.success(data)
            } else {
                val err = try {
                    val map = gson.fromJson(respBody, Map::class.java)
                    map["detail"] as? String ?: respBody
                } catch (e: Exception) {
                    respBody
                }
                Result.failure(Exception(err))
            }
        } catch (e: Exception) {
            Result.failure(e)
        }
    }

    /** PUT /users/{id} — only send [role] when a super admin changes it (contract §2.7). */
    suspend fun updateUser(userId: String, name: String?, role: String? = null, phone: String? = null, isSuperAdmin: Boolean? = null): Result<Map<String, Any?>> = withContext(Dispatchers.IO) {
        try {
            val payload = mutableMapOf<String, Any>()
            if (isSuperAdmin != null) payload["is_super_admin"] = isSuperAdmin
            if (!name.isNullOrBlank()) payload["name"] = name.trim()
            if (!role.isNullOrBlank()) payload["role"] = role
            if (!phone.isNullOrBlank()) payload["phone"] = phone.trim()
            val body = gson.toJson(payload).toRequestBody(jsonMediaType)
            val request = buildRequest("${prefs.serverUrl}/users/$userId").put(body).build()
            val response = okHttpClient.newCall(request).execute()
            val respBody = response.body?.string() ?: ""
            if (response.isSuccessful) {
                val mapType = object : TypeToken<Map<String, Any?>>() {}.type
                val data: Map<String, Any?> = gson.fromJson(respBody, mapType)
                Result.success(data)
            } else {
                val err = try {
                    val map = gson.fromJson(respBody, Map::class.java)
                    map["detail"] as? String ?: respBody
                } catch (e: Exception) {
                    respBody
                }
                Result.failure(Exception(err))
            }
        } catch (e: Exception) {
            Result.failure(e)
        }
    }

    suspend fun getRecordingDetail(recordingId: String): Result<Map<String, Any?>> = withContext(Dispatchers.IO) {
        try {
            val request = buildRequest("${prefs.serverUrl}/recordings/$recordingId").get().build()
            val response = okHttpClient.newCall(request).execute()
            val body = response.body?.string() ?: ""
            if (response.isSuccessful) {
                val mapType = object : TypeToken<Map<String, Any?>>() {}.type
                val data: Map<String, Any?> = gson.fromJson(body, mapType)
                Result.success(data)
            } else {
                Result.failure(Exception("Detail failed: $body"))
            }
        } catch (e: Exception) {
            Result.failure(e)
        }
    }

    suspend fun getGeminiUsage(recordingId: String): Result<Map<String, Any?>> = withContext(Dispatchers.IO) {
        try {
            val request = buildRequest("${prefs.serverUrl}/recordings/$recordingId/gemini-usage").get().build()
            val response = okHttpClient.newCall(request).execute()
            val body = response.body?.string() ?: ""
            if (response.isSuccessful) {
                val mapType = object : TypeToken<Map<String, Any?>>() {}.type
                val data: Map<String, Any?> = gson.fromJson(body, mapType)
                Result.success(data)
            } else {
                Result.failure(Exception("Usage failed: $body"))
            }
        } catch (e: Exception) {
            Result.failure(e)
        }
    }

    suspend fun toggleStar(recordingId: String, otpCode: String? = null): Result<Map<String, Any?>> = withContext(Dispatchers.IO) {
        try {
            val payload = mutableMapOf<String, Any>()
            if (!otpCode.isNullOrBlank()) payload["otp_code"] = otpCode.trim()
            val body = gson.toJson(payload).toRequestBody(jsonMediaType)
            val request = buildRequest("${prefs.serverUrl}/recordings/$recordingId/star").post(body).build()
            val response = okHttpClient.newCall(request).execute()
            val respBody = response.body?.string() ?: ""
            if (response.isSuccessful) {
                val mapType = object : TypeToken<Map<String, Any?>>() {}.type
                val data: Map<String, Any?> = gson.fromJson(respBody, mapType)
                Result.success(data)
            } else {
                val err = try {
                    val map = gson.fromJson(respBody, Map::class.java)
                    map["detail"] as? String ?: respBody
                } catch (e: Exception) {
                    respBody
                }
                Result.failure(Exception(err))
            }
        } catch (e: Exception) {
            Result.failure(e)
        }
    }

    suspend fun deleteRecording(recordingId: String): Result<Boolean> = withContext(Dispatchers.IO) {
        try {
            val request = buildRequest("${prefs.serverUrl}/recordings/$recordingId").delete().build()
            val response = okHttpClient.newCall(request).execute()
            val body = response.body?.string() ?: ""
            if (response.isSuccessful) {
                Result.success(true)
            } else {
                val err = try {
                    val map = gson.fromJson(body, Map::class.java)
                    map["detail"] as? String ?: body
                } catch (e: Exception) {
                    body
                }
                Result.failure(Exception(err))
            }
        } catch (e: Exception) {
            Result.failure(e)
        }
    }

    suspend fun reTranscribe(recordingId: String): Result<Boolean> = withContext(Dispatchers.IO) {
        try {
            val request = buildRequest("${prefs.serverUrl}/recordings/$recordingId/re-transcribe").post("{}".toRequestBody(jsonMediaType)).build()
            val response = okHttpClient.newCall(request).execute()
            val body = response.body?.string() ?: ""
            if (response.isSuccessful) {
                Result.success(true)
            } else {
                val err = try {
                    val map = gson.fromJson(body, Map::class.java)
                    map["detail"] as? String ?: body
                } catch (e: Exception) {
                    body
                }
                Result.failure(Exception(err))
            }
        } catch (e: Exception) {
            Result.failure(e)
        }
    }

    suspend fun mergeRecordings(recordingIds: List<String>, title: String? = null): Result<Map<String, Any?>> = withContext(Dispatchers.IO) {
        try {
            val payload = mutableMapOf<String, Any>(
                "recording_ids" to recordingIds
            )
            if (!title.isNullOrBlank()) {
                payload["title"] = title
            }
            val body = gson.toJson(payload).toRequestBody(jsonMediaType)
            val request = buildRequest("${prefs.serverUrl}/recordings/merge").post(body).build()
            val response = okHttpClient.newCall(request).execute()
            val responseBody = response.body?.string() ?: ""
            if (response.isSuccessful) {
                val mapType = object : TypeToken<Map<String, Any?>>() {}.type
                val data: Map<String, Any?> = gson.fromJson(responseBody, mapType)
                Result.success(data)
            } else {
                val cleanError = try {
                    val map = gson.fromJson(responseBody, Map::class.java)
                    map["detail"] as? String ?: responseBody
                } catch (e: Exception) {
                    responseBody
                }
                Result.failure(Exception(cleanError))
            }
        } catch (e: Exception) {
            Result.failure(e)
        }
    }

    suspend fun updateRecordingTitle(recordingId: String, newTitle: String): Result<Map<String, Any?>> = withContext(Dispatchers.IO) {
        try {
            val payload = mapOf("title" to newTitle.trim())
            val body = gson.toJson(payload).toRequestBody(jsonMediaType)
            val request = buildRequest("${prefs.serverUrl}/recordings/$recordingId/title").patch(body).build()
            val response = okHttpClient.newCall(request).execute()
            val responseBody = response.body?.string() ?: ""
            if (response.isSuccessful) {
                val mapType = object : TypeToken<Map<String, Any?>>() {}.type
                val data: Map<String, Any?> = gson.fromJson(responseBody, mapType)
                Result.success(data)
            } else {
                val cleanError = try {
                    val map = gson.fromJson(responseBody, Map::class.java)
                    map["detail"] as? String ?: responseBody
                } catch (e: Exception) {
                    responseBody
                }
                Result.failure(Exception(cleanError))
            }
        } catch (e: Exception) {
            Result.failure(e)
        }
    }

    // ── AI Chat Endpoints ───────────────────────────────────────────────────────

    suspend fun sendSessionChat(
        recordingId: String,
        message: String,
        templateId: String? = null
    ): Result<Map<String, Any?>> = withContext(Dispatchers.IO) {
        try {
            val payload = mutableMapOf<String, Any?>("message" to message)
            if (!templateId.isNullOrBlank()) {
                payload["template_id"] = templateId
            }
            val body = gson.toJson(payload).toRequestBody(jsonMediaType)
            val request = buildRequest("${prefs.serverUrl}/chat/session/$recordingId/message")
                .post(body)
                .build()
            val response = okHttpClient.newCall(request).execute()
            val responseBody = response.body?.string() ?: ""
            if (response.isSuccessful) {
                val mapType = object : TypeToken<Map<String, Any?>>() {}.type
                val data: Map<String, Any?> = gson.fromJson(responseBody, mapType)
                Result.success(data)
            } else {
                val cleanError = try {
                    val map = gson.fromJson(responseBody, Map::class.java)
                    map["detail"] as? String ?: responseBody
                } catch (e: Exception) {
                    responseBody
                }
                Result.failure(Exception(cleanError))
            }
        } catch (e: Exception) {
            Result.failure(e)
        }
    }

    suspend fun sendAllSessionsChat(
        message: String,
        templateId: String? = null
    ): Result<Map<String, Any?>> = withContext(Dispatchers.IO) {
        try {
            val payload = mutableMapOf<String, Any?>("message" to message)
            if (!templateId.isNullOrBlank()) {
                payload["template_id"] = templateId
            }
            val body = gson.toJson(payload).toRequestBody(jsonMediaType)
            val request = buildRequest("${prefs.serverUrl}/chat/all-sessions/message")
                .post(body)
                .build()
            val response = okHttpClient.newCall(request).execute()
            val responseBody = response.body?.string() ?: ""
            if (response.isSuccessful) {
                val mapType = object : TypeToken<Map<String, Any?>>() {}.type
                val data: Map<String, Any?> = gson.fromJson(responseBody, mapType)
                Result.success(data)
            } else {
                val cleanError = try {
                    val map = gson.fromJson(responseBody, Map::class.java)
                    map["detail"] as? String ?: responseBody
                } catch (e: Exception) {
                    responseBody
                }
                Result.failure(Exception(cleanError))
            }
        } catch (e: Exception) {
            Result.failure(e)
        }
    }

    suspend fun getChatHistory(
        recordingId: String?,
        mode: String
    ): Result<List<Map<String, Any?>>> = withContext(Dispatchers.IO) {
        try {
            val endpoint = if (mode == "session" && !recordingId.isNullOrBlank()) {
                "${prefs.serverUrl}/chat/session/$recordingId/history"
            } else {
                "${prefs.serverUrl}/chat/all-sessions/history"
            }
            val request = buildRequest(endpoint).get().build()
            val response = okHttpClient.newCall(request).execute()
            val responseBody = response.body?.string() ?: ""
            if (response.isSuccessful) {
                val listType = object : TypeToken<List<Map<String, Any?>>>() {}.type
                val data: List<Map<String, Any?>> = gson.fromJson(responseBody, listType)
                Result.success(data)
            } else {
                val cleanError = try {
                    val map = gson.fromJson(responseBody, Map::class.java)
                    map["detail"] as? String ?: responseBody
                } catch (e: Exception) {
                    responseBody
                }
                Result.failure(Exception(cleanError))
            }
        } catch (e: Exception) {
            Result.failure(e)
        }
    }

    suspend fun listChatTemplates(): Result<List<Map<String, Any?>>> = withContext(Dispatchers.IO) {
        try {
            val request = buildRequest("${prefs.serverUrl}/chat/templates").get().build()
            val response = okHttpClient.newCall(request).execute()
            val responseBody = response.body?.string() ?: ""
            if (response.isSuccessful) {
                val listType = object : TypeToken<List<Map<String, Any?>>>() {}.type
                val data: List<Map<String, Any?>> = gson.fromJson(responseBody, listType)
                Result.success(data)
            } else {
                val cleanError = try {
                    val map = gson.fromJson(responseBody, Map::class.java)
                    map["detail"] as? String ?: responseBody
                } catch (e: Exception) {
                    responseBody
                }
                Result.failure(Exception(cleanError))
            }
        } catch (e: Exception) {
            Result.failure(e)
        }
    }

    suspend fun createChatTemplate(
        title: String,
        prompt: String,
        exampleOutput: String? = null,
        icon: String = "📋",
        sortOrder: Int = 0
    ): Result<Map<String, Any?>> = withContext(Dispatchers.IO) {
        try {
            val payload = mutableMapOf<String, Any?>(
                "title" to title.trim(),
                "prompt" to prompt.trim(),
                "icon" to icon,
                "sort_order" to sortOrder
            )
            if (!exampleOutput.isNullOrBlank()) {
                payload["example_output"] = exampleOutput.trim()
            }
            val body = gson.toJson(payload).toRequestBody(jsonMediaType)
            val request = buildRequest("${prefs.serverUrl}/chat/templates").post(body).build()
            val response = okHttpClient.newCall(request).execute()
            val responseBody = response.body?.string() ?: ""
            if (response.isSuccessful) {
                val mapType = object : TypeToken<Map<String, Any?>>() {}.type
                val data: Map<String, Any?> = gson.fromJson(responseBody, mapType)
                Result.success(data)
            } else {
                val cleanError = try {
                    val map = gson.fromJson(responseBody, Map::class.java)
                    map["detail"] as? String ?: responseBody
                } catch (e: Exception) {
                    responseBody
                }
                Result.failure(Exception(cleanError))
            }
        } catch (e: Exception) {
            Result.failure(e)
        }
    }

    suspend fun updateChatTemplate(
        id: String,
        title: String? = null,
        prompt: String? = null,
        exampleOutput: String? = null,
        icon: String? = null,
        isActive: Boolean? = null,
        sortOrder: Int? = null
    ): Result<Map<String, Any?>> = withContext(Dispatchers.IO) {
        try {
            val payload = mutableMapOf<String, Any?>()
            if (title != null) payload["title"] = title.trim()
            if (prompt != null) payload["prompt"] = prompt.trim()
            if (exampleOutput != null) payload["example_output"] = exampleOutput.trim()
            if (icon != null) payload["icon"] = icon
            if (isActive != null) payload["is_active"] = isActive
            if (sortOrder != null) payload["sort_order"] = sortOrder

            val body = gson.toJson(payload).toRequestBody(jsonMediaType)
            val request = buildRequest("${prefs.serverUrl}/chat/templates/$id").put(body).build()
            val response = okHttpClient.newCall(request).execute()
            val responseBody = response.body?.string() ?: ""
            if (response.isSuccessful) {
                val mapType = object : TypeToken<Map<String, Any?>>() {}.type
                val data: Map<String, Any?> = gson.fromJson(responseBody, mapType)
                Result.success(data)
            } else {
                val cleanError = try {
                    val map = gson.fromJson(responseBody, Map::class.java)
                    map["detail"] as? String ?: responseBody
                } catch (e: Exception) {
                    responseBody
                }
                Result.failure(Exception(cleanError))
            }
        } catch (e: Exception) {
            Result.failure(e)
        }
    }

    suspend fun deleteChatTemplate(id: String): Result<Boolean> = withContext(Dispatchers.IO) {
        try {
            val request = buildRequest("${prefs.serverUrl}/chat/templates/$id").delete().build()
            val response = okHttpClient.newCall(request).execute()
            if (response.isSuccessful) {
                Result.success(true)
            } else {
                val responseBody = response.body?.string() ?: ""
                val cleanError = try {
                    val map = gson.fromJson(responseBody, Map::class.java)
                    map["detail"] as? String ?: responseBody
                } catch (e: Exception) {
                    responseBody
                }
                Result.failure(Exception(cleanError))
            }
        } catch (e: Exception) {
            Result.failure(e)
        }
    }

    // ── AI Model Configuration ──────────────────────────────────────────────────

    suspend fun getAIModelConfig(): Result<Map<String, Any?>> = withContext(Dispatchers.IO) {
        try {
            val request = buildRequest("${prefs.serverUrl}/settings/ai-models").get().build()
            val response = okHttpClient.newCall(request).execute()
            val body = response.body?.string() ?: ""
            if (response.isSuccessful) {
                val mapType = object : TypeToken<Map<String, Any?>>() {}.type
                val data: Map<String, Any?> = gson.fromJson(body, mapType)
                Result.success(data)
            } else {
                Result.failure(Exception("Failed to load AI model config: $body"))
            }
        } catch (e: Exception) {
            Result.failure(e)
        }
    }

    suspend fun getAvailableModels(): Result<List<Map<String, Any?>>> = withContext(Dispatchers.IO) {
        try {
            val request = buildRequest("${prefs.serverUrl}/settings/ai-models/available").get().build()
            val response = okHttpClient.newCall(request).execute()
            val body = response.body?.string() ?: ""
            if (response.isSuccessful) {
                val mapType = object : TypeToken<Map<String, Any?>>() {}.type
                val data: Map<String, Any?> = gson.fromJson(body, mapType)
                @Suppress("UNCHECKED_CAST")
                val models = data["models"] as? List<Map<String, Any?>> ?: emptyList()
                Result.success(models)
            } else {
                Result.failure(Exception("Failed to load available models: $body"))
            }
        } catch (e: Exception) {
            Result.failure(e)
        }
    }

    suspend fun updateAIModel(
        transcriptionModel: String? = null,
        intelligenceModel: String? = null
    ): Result<Map<String, Any?>> = withContext(Dispatchers.IO) {
        try {
            val payload = mutableMapOf<String, Any?>()
            if (!transcriptionModel.isNullOrBlank()) payload["transcription_model"] = transcriptionModel
            if (!intelligenceModel.isNullOrBlank()) payload["intelligence_model"] = intelligenceModel
            val body = gson.toJson(payload).toRequestBody(jsonMediaType)
            val request = buildRequest("${prefs.serverUrl}/settings/ai-models").put(body).build()
            val response = okHttpClient.newCall(request).execute()
            val respBody = response.body?.string() ?: ""
            if (response.isSuccessful) {
                val mapType = object : TypeToken<Map<String, Any?>>() {}.type
                val data: Map<String, Any?> = gson.fromJson(respBody, mapType)
                Result.success(data)
            } else {
                val err = try {
                    val map = gson.fromJson(respBody, Map::class.java)
                    map["detail"] as? String ?: respBody
                } catch (e: Exception) {
                    respBody
                }
                Result.failure(Exception(err))
            }
        } catch (e: Exception) {
            Result.failure(e)
        }
    }

    // ── Model testing: upload audio, transcript versions per model ─────────────

    private fun errorDetail(body: String, fallback: String): String = try {
        val map = gson.fromJson(body, Map::class.java)
        (map["detail"] as? String) ?: fallback
    } catch (e: Exception) {
        fallback
    }

    /** Streams a picked file (content Uri) to the server, reporting progress 0..1. */
    private class UriRequestBody(
        private val resolver: ContentResolver,
        private val uri: Uri,
        private val mediaType: MediaType?,
        private val length: Long,
        private val onProgress: (Float) -> Unit
    ) : RequestBody() {
        override fun contentType(): MediaType? = mediaType
        override fun contentLength(): Long = if (length > 0) length else -1L
        override fun writeTo(sink: BufferedSink) {
            val input = resolver.openInputStream(uri) ?: throw IOException("Cannot open the selected file")
            input.use { stream ->
                val buffer = ByteArray(64 * 1024)
                var sent = 0L
                while (true) {
                    val read = stream.read(buffer)
                    if (read == -1) break
                    sink.write(buffer, 0, read)
                    sent += read
                    if (length > 0) onProgress((sent.toFloat() / length).coerceIn(0f, 1f))
                }
            }
        }
    }

    suspend fun importAudio(
        uri: Uri,
        fileName: String,
        sizeBytes: Long,
        model: String,
        title: String?,
        onProgress: (Float) -> Unit
    ): Result<Map<String, Any?>> = withContext(Dispatchers.IO) {
        try {
            val mime = appContext.contentResolver.getType(uri) ?: "application/octet-stream"
            val fileBody = UriRequestBody(appContext.contentResolver, uri, mime.toMediaType(), sizeBytes, onProgress)
            val form = MultipartBody.Builder()
                .setType(MultipartBody.FORM)
                .addFormDataPart("model", model)
                .apply { if (!title.isNullOrBlank()) addFormDataPart("title", title) }
                .addFormDataPart("file", fileName, fileBody)
                .build()
            val uploadClient = okHttpClient.newBuilder()
                .writeTimeout(20, TimeUnit.MINUTES)
                .readTimeout(5, TimeUnit.MINUTES)
                .retryOnConnectionFailure(false)
                .build()
            val request = buildRequest("${prefs.serverUrl}/recordings/import").post(form).build()
            val response = uploadClient.newCall(request).execute()
            val body = response.body?.string() ?: ""
            if (response.isSuccessful) {
                val mapType = object : TypeToken<Map<String, Any?>>() {}.type
                Result.success(gson.fromJson<Map<String, Any?>>(body, mapType))
            } else {
                Result.failure(Exception(errorDetail(body, "Upload failed (${response.code})")))
            }
        } catch (e: Exception) {
            Result.failure(e)
        }
    }

    suspend fun reTranscribeWithModel(recordingId: String, model: String): Result<Boolean> = withContext(Dispatchers.IO) {
        try {
            val body = gson.toJson(mapOf("model" to model)).toRequestBody(jsonMediaType)
            val request = buildRequest("${prefs.serverUrl}/recordings/$recordingId/re-transcribe").post(body).build()
            val response = okHttpClient.newCall(request).execute()
            val respBody = response.body?.string() ?: ""
            if (response.isSuccessful) Result.success(true)
            else Result.failure(Exception(errorDetail(respBody, "Could not start (${response.code})")))
        } catch (e: Exception) {
            Result.failure(e)
        }
    }

    suspend fun getTranscriptRuns(recordingId: String): Result<Map<String, Any?>> = withContext(Dispatchers.IO) {
        try {
            val request = buildRequest("${prefs.serverUrl}/recordings/$recordingId/runs").get().build()
            val response = okHttpClient.newCall(request).execute()
            val body = response.body?.string() ?: ""
            if (response.isSuccessful) {
                val mapType = object : TypeToken<Map<String, Any?>>() {}.type
                Result.success(gson.fromJson<Map<String, Any?>>(body, mapType))
            } else {
                Result.failure(Exception(errorDetail(body, "Could not load versions (${response.code})")))
            }
        } catch (e: Exception) {
            Result.failure(e)
        }
    }

    suspend fun getTranscriptRun(recordingId: String, runId: String): Result<Map<String, Any?>> = withContext(Dispatchers.IO) {
        try {
            val request = buildRequest("${prefs.serverUrl}/recordings/$recordingId/runs/$runId").get().build()
            val response = okHttpClient.newCall(request).execute()
            val body = response.body?.string() ?: ""
            if (response.isSuccessful) {
                val mapType = object : TypeToken<Map<String, Any?>>() {}.type
                Result.success(gson.fromJson<Map<String, Any?>>(body, mapType))
            } else {
                Result.failure(Exception(errorDetail(body, "Could not load this version (${response.code})")))
            }
        } catch (e: Exception) {
            Result.failure(e)
        }
    }

    suspend fun activateTranscriptRun(recordingId: String, runId: String): Result<Boolean> = withContext(Dispatchers.IO) {
        try {
            val request = buildRequest("${prefs.serverUrl}/recordings/$recordingId/runs/$runId/activate")
                .post("{}".toRequestBody(jsonMediaType)).build()
            val response = okHttpClient.newCall(request).execute()
            val body = response.body?.string() ?: ""
            if (response.isSuccessful) Result.success(true)
            else Result.failure(Exception(errorDetail(body, "Could not switch (${response.code})")))
        } catch (e: Exception) {
            Result.failure(e)
        }
    }

    // ═════════════════════════════════════════════════════════════════════════
    // Jomi v2 endpoints (docs/API_CONTRACT_v2.md §2–§6)
    // ═════════════════════════════════════════════════════════════════════════

    private val mapTypeV2 = object : TypeToken<Map<String, Any?>>() {}.type

    private val longTimeoutClient: OkHttpClient by lazy {
        okHttpClient.newBuilder().readTimeout(180, TimeUnit.SECONDS).build()
    }

    /** Builds "<server><path>?k=v" with proper escaping; blank/null query values are skipped. */
    private fun apiUrl(path: String, query: Map<String, Any?> = emptyMap()): String {
        val base = "${prefs.serverUrl}$path"
        val parsed = base.toHttpUrlOrNull() ?: return base
        val builder = parsed.newBuilder()
        query.forEach { (k, v) ->
            if (v != null && v.toString().isNotBlank()) builder.addQueryParameter(k, v.toString())
        }
        return builder.build().toString()
    }

    private fun friendlyHttpError(code: Int): String = when (code) {
        400 -> "The server could not accept this request"
        401 -> "Please sign in again"
        403 -> "You don't have permission to do that"
        404 -> "Not found"
        409 -> "That conflicts with the current state — refresh and try again"
        410 -> "This item no longer exists"
        422 -> "Some details are missing or invalid"
        in 500..599 -> "Server error ($code). Please try again"
        else -> "Request failed ($code)"
    }

    /** One JSON call. Non-2xx → [ApiException] with the FastAPI "detail" text. */
    private suspend fun send(
        method: String,
        url: String,
        payload: Any? = null,
        longTimeout: Boolean = false
    ): Result<String> = withContext(Dispatchers.IO) {
        try {
            val body = when {
                payload != null -> gson.toJson(payload).toRequestBody(jsonMediaType)
                method == "POST" || method == "PUT" || method == "PATCH" -> "{}".toRequestBody(jsonMediaType)
                else -> null
            }
            val request = buildRequest(url).method(method, body).build()
            val client = if (longTimeout) longTimeoutClient else okHttpClient
            client.newCall(request).execute().use { response ->
                val text = response.body?.string() ?: ""
                if (response.isSuccessful) {
                    Result.success(text)
                } else {
                    val parsed: Map<String, Any?>? = try { gson.fromJson(text, mapTypeV2) } catch (e: Exception) { null }
                    val detail = when (val d = parsed?.get("detail")) {
                        is String -> d
                        else -> null
                    }
                    Result.failure(ApiException(response.code, detail?.takeIf { it.isNotBlank() } ?: friendlyHttpError(response.code), parsed))
                }
            }
        } catch (e: IOException) {
            Result.failure(IOException("Can't reach the server. Check your internet connection.", e))
        } catch (e: Exception) {
            Result.failure(e)
        }
    }

    private fun parseMap(text: String): Map<String, Any?> =
        if (text.isBlank()) emptyMap() else (gson.fromJson<Map<String, Any?>>(text, mapTypeV2) ?: emptyMap())

    /** Accepts a bare JSON array or an object holding the list under one of [keys]. */
    @Suppress("UNCHECKED_CAST")
    private fun parseList(text: String, vararg keys: String): List<Map<String, Any?>> {
        val trimmed = text.trim()
        if (trimmed.isEmpty()) return emptyList()
        if (trimmed.startsWith("[")) {
            val listType = object : TypeToken<List<Map<String, Any?>>>() {}.type
            return gson.fromJson<List<Map<String, Any?>>>(trimmed, listType) ?: emptyList()
        }
        val map = parseMap(trimmed)
        for (k in keys) {
            (map[k] as? List<Map<String, Any?>>)?.let { return it }
        }
        return emptyList()
    }

    private suspend fun sendForMap(method: String, url: String, payload: Any? = null, longTimeout: Boolean = false) =
        send(method, url, payload, longTimeout).mapCatching { parseMap(it) }

    // ── Auth (§2.2) ─────────────────────────────────────────────────────────────

    /** GET /auth/me — also refreshes the cached profile (role, super admin, role label). */
    suspend fun getMe(): Result<Map<String, Any?>> =
        sendForMap("GET", apiUrl("/auth/me")).onSuccess { prefs.updateProfile(it) }

    /** GET /app/latest (no auth) — newest build for the "Update available" prompt. */
    suspend fun getLatestApp(): Result<Map<String, Any?>> = sendForMap("GET", apiUrl("/app/latest"))

    /** PATCH /auth/me {name} — set your own name (first login). Refreshes the cached profile. */
    suspend fun updateMyName(name: String): Result<Map<String, Any?>> =
        sendForMap("PATCH", apiUrl("/auth/me"), mapOf("name" to name.trim())).onSuccess { prefs.updateProfile(it) }

    // ── Users (§2.4–§2.9) ──────────────────────────────────────────────────────

    /** GET /users/check — possible duplicates by phone (last 10 digits) or exact name, archived included. */
    suspend fun checkUser(name: String?, phone: String?): Result<List<Map<String, Any?>>> =
        send("GET", apiUrl("/users/check", mapOf("name" to name?.trim(), "phone" to phone?.trim())))
            .mapCatching { parseList(it, "matches") }

    suspend fun archiveUser(userId: String): Result<Map<String, Any?>> =
        sendForMap("POST", apiUrl("/users/$userId/archive"))

    suspend fun unarchiveUser(userId: String): Result<Map<String, Any?>> =
        sendForMap("POST", apiUrl("/users/$userId/unarchive"))

    /** DELETE /users/{id}?permanent=true → {"status":"deleted","recordings_kept":N}. */
    suspend fun deleteUserPermanently(userId: String): Result<Map<String, Any?>> =
        sendForMap("DELETE", apiUrl("/users/$userId", mapOf("permanent" to "true")))

    // ── Recorder config & lists (§3) ───────────────────────────────────────────

    /** GET /settings/recorder-config. Cached in prefs so dropdowns work offline. */
    suspend fun getRecorderConfig(): Result<Map<String, Any?>> =
        send("GET", apiUrl("/settings/recorder-config")).mapCatching { text ->
            val map = parseMap(text)
            prefs.recorderConfigJson = text
            map["role_label"]?.toString()?.takeIf { it.isNotBlank() }?.let { prefs.roleLabel = it }
            (map["is_super_admin"] as? Boolean)?.let { prefs.isSuperAdmin = it }
            map
        }

    /** Last cached recorder config (may be stale); null if never loaded. */
    fun cachedRecorderConfig(): Map<String, Any?>? = try {
        prefs.recorderConfigJson?.let { parseMap(it) }
    } catch (e: Exception) {
        null
    }

    /** PUT /settings/recorder-config (admin). [reportTemplates] = {"schedule": templateId, "meeting": templateId}. */
    suspend fun updateRecorderConfig(
        reportTemplates: Map<String, String>? = null,
        roleLabel: String? = null,
        defaultMode: String? = null,
        silenceStopMinutes: Map<String, Int>? = null
    ): Result<Map<String, Any?>> {
        val payload = mutableMapOf<String, Any?>()
        reportTemplates?.let { payload["report_templates"] = it }
        silenceStopMinutes?.let { payload["silence_stop_minutes"] = it }
        roleLabel?.let { payload["role_label"] = it }
        defaultMode?.let { payload["default_mode"] = it }
        return sendForMap("PUT", apiUrl("/settings/recorder-config"), payload)
    }

    /** POST /settings/report-templates/restore-defaults (admin) → {created: [...], mapped: {...}, config: {...}}. */
    suspend fun restoreDefaultReportTemplates(): Result<Map<String, Any?>> =
        sendForMap("POST", apiUrl("/settings/report-templates/restore-defaults"))

    /** GET /settings/lists/{list_type} — list_type ∈ {balgruh, coach_name}. */
    suspend fun listListItems(listType: String, includeInactive: Boolean = true): Result<List<Map<String, Any?>>> =
        send("GET", apiUrl("/settings/lists/$listType", mapOf("include_inactive" to if (includeInactive) "true" else null)))
            .mapCatching { parseList(it, "items", listType) }

    suspend fun createListItem(listType: String, name: String): Result<Map<String, Any?>> =
        sendForMap("POST", apiUrl("/settings/lists/$listType"), mapOf("name" to name.trim()))

    suspend fun updateListItem(
        listType: String,
        id: String,
        name: String? = null,
        isActive: Boolean? = null,
        sortOrder: Int? = null
    ): Result<Map<String, Any?>> {
        val payload = mutableMapOf<String, Any?>()
        name?.let { payload["name"] = it.trim() }
        isActive?.let { payload["is_active"] = it }
        sortOrder?.let { payload["sort_order"] = it }
        return sendForMap("PUT", apiUrl("/settings/lists/$listType/$id"), payload)
    }

    /** DELETE /settings/lists/{type}/{id} — server marks the item inactive. */
    suspend fun deleteListItem(listType: String, id: String): Result<Map<String, Any?>> =
        sendForMap("DELETE", apiUrl("/settings/lists/$listType/$id"))

    // ── Recordings (§4) ─────────────────────────────────────────────────────────

    /** DELETE /recordings/{id} — erases audio, transcript, report and chats for real (§4.8). */
    suspend fun eraseRecording(recordingId: String): Result<Map<String, Any?>> =
        sendForMap("DELETE", apiUrl("/recordings/$recordingId"))

    /** Admin: sessions that were never finished (app closed / phone killed) and are not live. */
    suspend fun listUnfinishedSessions(): Result<List<Map<String, Any?>>> =
        send("GET", apiUrl("/recordings/unfinished")).mapCatching { parseList(it, "items") }

    /** Admin: finish a stuck session now (uploads received so far are transcribed; none = abandoned). */
    suspend fun forceFinishSession(recordingId: String): Result<Map<String, Any?>> =
        sendForMap("POST", apiUrl("/recordings/$recordingId/force-finish"))

    /** Authenticated audio URL (use with the Authorization header, never ?token=). */
    fun audioUrl(recordingId: String): String = "${prefs.serverUrl}/recordings/$recordingId/audio"

    // ── Reports (§5) ────────────────────────────────────────────────────────────

    /** GET /recordings/{id}/report. Success(null) when no report exists yet (404). */
    suspend fun getReport(recordingId: String): Result<Map<String, Any?>?> {
        val res = send("GET", apiUrl("/recordings/$recordingId/report"))
        val err = res.exceptionOrNull()
        if (err is ApiException && err.code == 404) return Result.success(null)
        return res.mapCatching { parseMap(it) }
    }

    suspend fun saveReport(recordingId: String, content: String): Result<Map<String, Any?>> =
        sendForMap("PUT", apiUrl("/recordings/$recordingId/report"), mapOf("content" to content))

    suspend fun regenerateReport(recordingId: String, templateId: String? = null): Result<Map<String, Any?>> {
        val payload = mutableMapOf<String, Any?>()
        if (!templateId.isNullOrBlank()) payload["template_id"] = templateId
        return sendForMap("POST", apiUrl("/recordings/$recordingId/report/regenerate"), payload)
    }

    suspend fun finalizeReport(recordingId: String): Result<Map<String, Any?>> =
        sendForMap("POST", apiUrl("/recordings/$recordingId/report/finalize"))

    /** Admin only: final → draft. */
    suspend fun reopenReport(recordingId: String): Result<Map<String, Any?>> =
        sendForMap("POST", apiUrl("/recordings/$recordingId/report/reopen"))

    /** POST …/report/ai-edit → the proposed text. Nothing is saved on the server. */
    suspend fun aiEditReport(recordingId: String, instruction: String, content: String? = null): Result<String> {
        val payload = mutableMapOf<String, Any?>("instruction" to instruction.trim())
        if (content != null) payload["content"] = content
        return sendForMap("POST", apiUrl("/recordings/$recordingId/report/ai-edit"), payload, longTimeout = true)
            .mapCatching { it["proposed_content"]?.toString() ?: throw IllegalStateException("The AI did not return a proposal") }
    }

    // ── Calls (14 Oct): the phone's own call recordings, private to the owner ──────

    /** POST /calls/upload: one call file from the phone. Returns {status: processing|duplicate, recording_id}. */
    suspend fun uploadCall(
        uri: Uri, fileName: String, sizeBytes: Long, contactName: String?, contactPhone: String?,
        callTimeMs: Long?, callApp: String?, fileKey: String
    ): Result<Map<String, Any?>> = withContext(Dispatchers.IO) {
        try {
            val mime = appContext.contentResolver.getType(uri) ?: "audio/mp4"
            val form = MultipartBody.Builder().setType(MultipartBody.FORM)
                .apply {
                    contactName?.let { addFormDataPart("contact_name", it) }
                    contactPhone?.let { addFormDataPart("contact_phone", it) }
                    callTimeMs?.let { addFormDataPart("call_started_at", it.toString()) }
                    callApp?.let { addFormDataPart("call_app", it) }
                }
                .addFormDataPart("file_key", fileKey)
                .addFormDataPart("file", fileName, UriRequestBody(appContext.contentResolver, uri, mime.toMediaType(), sizeBytes) {})
                .build()
            val client = okHttpClient.newBuilder().writeTimeout(10, TimeUnit.MINUTES).readTimeout(2, TimeUnit.MINUTES).build()
            val response = client.newCall(buildRequest("${prefs.serverUrl}/calls/upload").post(form).build()).execute()
            val body = response.body?.string() ?: ""
            if (response.isSuccessful) {
                val mapType = object : TypeToken<Map<String, Any?>>() {}.type
                Result.success(gson.fromJson<Map<String, Any?>>(body, mapType))
            } else Result.failure(Exception(errorDetail(body, "Call upload failed (${response.code})")))
        } catch (e: Exception) {
            Result.failure(e)
        }
    }

    /** GET /recordings/?source=call — my calls (admins: + calls shared with them, or only those with [sharedOnly]). */
    suspend fun listCalls(page: Int = 1, limit: Int = 20, contact: String? = null, dateFrom: String? = null,
                          dateTo: String? = null, sharedOnly: Boolean = false): Result<Map<String, Any?>> =
        sendForMap("GET", apiUrl("/recordings/", mapOf("source" to "call", "page" to page, "limit" to limit,
            "contact" to contact, "date_from" to dateFrom, "date_to" to dateTo, "shared_only" to if (sharedOnly) "true" else null)))

    /** GET /calls/contacts → [{name, calls, last_call}] */
    suspend fun callContacts(): Result<List<Map<String, Any?>>> =
        send("GET", apiUrl("/calls/contacts")).mapCatching { parseList(it, "items") }

    /** POST /calls/{id}/share {shared} — owner only. */
    suspend fun shareCall(recordingId: String, shared: Boolean): Result<Map<String, Any?>> =
        sendForMap("POST", apiUrl("/calls/$recordingId/share"), mapOf("shared" to shared))

    /** GET /tasks?status=open|done|all&recording_id= */
    suspend fun listTasks(status: String = "open", recordingId: String? = null): Result<List<Map<String, Any?>>> =
        send("GET", apiUrl("/tasks", mapOf("status" to status, "recording_id" to recordingId))).mapCatching { parseList(it, "items") }

    suspend fun addTask(text: String, dueText: String? = null, recordingId: String? = null): Result<Map<String, Any?>> =
        sendForMap("POST", apiUrl("/tasks"), mapOf("text" to text, "due_text" to dueText, "recording_id" to recordingId))

    suspend fun updateTask(taskId: String, text: String? = null, dueText: String? = null, done: Boolean? = null): Result<Map<String, Any?>> =
        sendForMap("PUT", apiUrl("/tasks/$taskId"), buildMap {
            text?.let { put("text", it) }; dueText?.let { put("due_text", it) }; done?.let { put("done", it) }
        })

    suspend fun deleteTask(taskId: String): Result<Map<String, Any?>> = sendForMap("DELETE", apiUrl("/tasks/$taskId"))

    /** GET/PUT /task-reminder → {on, time, open_count} */
    suspend fun getTaskReminder(): Result<Map<String, Any?>> = sendForMap("GET", apiUrl("/task-reminder"))

    suspend fun setTaskReminder(on: Boolean? = null, time: String? = null): Result<Map<String, Any?>> =
        sendForMap("PUT", apiUrl("/task-reminder"), buildMap { on?.let { put("on", it) }; time?.let { put("time", it) } })

    /** POST /recordings/{id}/report/translate {lang: "hi" | "mr"} → the report in that language (saved on the server). */
    suspend fun translateReport(recordingId: String, lang: String): Result<String> =
        sendForMap("POST", apiUrl("/recordings/$recordingId/report/translate"), mapOf("lang" to lang), longTimeout = true)
            .mapCatching { it["content"]?.toString() ?: throw IllegalStateException("No translation returned") }

    // ── Director's POV (21 Oct, admins only) ────────────────────────────────────

    /** GET /recordings/{id}/director-pov → {status: none|generating|ready|error, content, generated_by_name, generated_at} */
    suspend fun getDirectorPov(recordingId: String): Result<Map<String, Any?>> =
        sendForMap("GET", apiUrl("/recordings/$recordingId/director-pov"))

    /** POST /recordings/{id}/director-pov — makes it (in the background) unless saved; [regenerate] makes a fresh one. */
    suspend fun makeDirectorPov(recordingId: String, regenerate: Boolean = false): Result<Map<String, Any?>> =
        sendForMap("POST", apiUrl("/recordings/$recordingId/director-pov", mapOf("regenerate" to regenerate)))

    // ── Phone notifications (21 Oct) ────────────────────────────────────────────

    /** GET /notifications/inbox → {items:[{id, kind, title, body, recording_id}], latest_id}; null = start from now. */
    suspend fun getNotificationInbox(afterId: Long?): Result<Map<String, Any?>> =
        sendForMap("GET", apiUrl("/notifications/inbox", mapOf("after_id" to afterId)))

    /** GET /notifications/settings → {items:[{kind, label, on}], can_announce} */
    suspend fun getNotificationSettings(): Result<Map<String, Any?>> = sendForMap("GET", apiUrl("/notifications/settings"))

    suspend fun setNotificationSettings(prefs: Map<String, Boolean>): Result<Map<String, Any?>> =
        sendForMap("PUT", apiUrl("/notifications/settings"), mapOf("prefs" to prefs))

    /** POST /notifications/announce (super admins) → {sent}. [audience] = all | coaches | admins */
    suspend fun sendAnnouncement(message: String, audience: String): Result<Int> =
        sendForMap("POST", apiUrl("/notifications/announce"), mapOf("message" to message, "audience" to audience))
            .mapCatching { (it["sent"] as? Number)?.toInt() ?: 0 }

    /** POST /recordings/{id}/report/share-link → the web link to the full report (made once, never expires). */
    suspend fun getReportShareLink(recordingId: String, lang: String? = null): Result<String> =
        sendForMap("POST", apiUrl("/recordings/$recordingId/report/share-link"), mapOf("lang" to lang))
            .mapCatching { it["url"]?.toString()?.takeIf { u -> u.startsWith("http") } ?: throw IllegalStateException("No link returned") }

    // ── Ask Jomi: saved conversations (§6) ─────────────────────────────────────

    /** GET /chat/conversations — own conversations, newest first. [mode] = "all_sessions" | "session". */
    suspend fun listConversations(mode: String? = null, recordingId: String? = null): Result<List<Map<String, Any?>>> =
        send("GET", apiUrl("/chat/conversations", mapOf("mode" to mode, "recording_id" to recordingId)))
            .mapCatching { parseList(it, "items", "conversations") }

    suspend fun createConversation(mode: String, recordingId: String? = null): Result<Map<String, Any?>> {
        val payload = mutableMapOf<String, Any?>("mode" to mode)
        if (!recordingId.isNullOrBlank()) payload["recording_id"] = recordingId
        return sendForMap("POST", apiUrl("/chat/conversations"), payload)
    }

    /** → {"conversation": {...}, "messages": [...]} */
    suspend fun getConversationMessages(conversationId: String): Result<Map<String, Any?>> =
        sendForMap("GET", apiUrl("/chat/conversations/$conversationId/messages"))

    /** → the assistant message (same shape as the legacy session message endpoint). */
    suspend fun sendConversationMessage(conversationId: String, message: String, templateId: String? = null): Result<Map<String, Any?>> {
        val payload = mutableMapOf<String, Any?>("message" to message)
        if (!templateId.isNullOrBlank()) payload["template_id"] = templateId
        return sendForMap("POST", apiUrl("/chat/conversations/$conversationId/message"), payload, longTimeout = true)
    }

    suspend fun renameConversation(conversationId: String, title: String): Result<Map<String, Any?>> =
        sendForMap("PATCH", apiUrl("/chat/conversations/$conversationId"), mapOf("title" to title.trim()))

    suspend fun deleteConversation(conversationId: String): Result<Map<String, Any?>> =
        sendForMap("DELETE", apiUrl("/chat/conversations/$conversationId"))
}
