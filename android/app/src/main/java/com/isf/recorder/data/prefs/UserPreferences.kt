package com.isf.recorder.data.prefs

import android.content.Context
import android.content.SharedPreferences

class UserPreferences(context: Context) {
    private val prefs: SharedPreferences = context.getSharedPreferences("isf_recorder_prefs", Context.MODE_PRIVATE)

    companion object {
        private const val KEY_JWT_TOKEN = "jwt_token"
        private const val KEY_USER_ID = "user_id"
        private const val KEY_USER_NAME = "user_name"
        private const val KEY_USER_PHONE = "user_phone"
        private const val KEY_USER_ROLE = "user_role"
        private const val KEY_ORG_ID = "org_id"
        private const val KEY_ORG_NAME = "org_name"
        private const val KEY_SERVER_URL = "server_url"
        private const val KEY_DEFAULT_MIC = "default_mic"
        private const val KEY_IS_SUPER_ADMIN = "is_super_admin"
        private const val KEY_NEEDS_NAME = "needs_name"
        private const val KEY_LAST_UPDATE_CHECK = "last_update_check_ms"
        private const val KEY_LAST_NOTIFIED_UPDATE = "last_notified_update_code"
        private const val KEY_VIEW_AS_COACH = "view_as_coach"
        private const val KEY_ROLE_LABEL = "role_label"
        private const val KEY_RECORDER_CONFIG_JSON = "recorder_config_json"

        // Production public domain with SSL; works on any mobile network (4G/5G/Wi-Fi)
        const val DEFAULT_SERVER_URL = "https://gubandhu.initiativesewafoundation.com/recorder"
        const val DEFAULT_ROLE_LABEL = "Coach"
    }

    var token: String?
        get() = prefs.getString(KEY_JWT_TOKEN, null)
        set(value) = prefs.edit().putString(KEY_JWT_TOKEN, value).apply()

    var userId: String?
        get() = prefs.getString(KEY_USER_ID, null)
        set(value) = prefs.edit().putString(KEY_USER_ID, value).apply()

    var userName: String?
        get() = prefs.getString(KEY_USER_NAME, null)
        set(value) = prefs.edit().putString(KEY_USER_NAME, value).apply()

    var userPhone: String?
        get() = prefs.getString(KEY_USER_PHONE, null)
        set(value) = prefs.edit().putString(KEY_USER_PHONE, value).apply()

    /** Raw DB role: "admin" | "staff". Never shown to users (see [roleLabel]). */
    var userRole: String
        get() = prefs.getString(KEY_USER_ROLE, "staff") ?: "staff"
        set(value) = prefs.edit().putString(KEY_USER_ROLE, value).apply()

    var orgId: String?
        get() = prefs.getString(KEY_ORG_ID, null)
        set(value) = prefs.edit().putString(KEY_ORG_ID, value).apply()

    var orgName: String?
        get() = prefs.getString(KEY_ORG_NAME, "ISF")
        set(value) = prefs.edit().putString(KEY_ORG_NAME, value).apply()

    var serverUrl: String
        // Fixed per build type (release / staging); a previously saved custom URL is ignored.
        get() = com.isf.recorder.BuildConfig.SERVER_URL.trimEnd('/')
        @Suppress("UNUSED_PARAMETER")
        set(value) { /* no-op: the server is fixed per build (core change, Round 2) */ }

    /** Phone is in SUPER_ADMIN_PHONES on the server (can change roles, can "view as Coach"). */
    var lastUpdateCheckMs: Long
        get() = prefs.getLong(KEY_LAST_UPDATE_CHECK, 0L)
        set(value) = prefs.edit().putLong(KEY_LAST_UPDATE_CHECK, value).apply()

    /** The newest version the "update available" phone notification was already shown for (once per version). */
    var lastNotifiedUpdateCode: Int
        get() = prefs.getInt(KEY_LAST_NOTIFIED_UPDATE, 0)
        set(value) = prefs.edit().putInt(KEY_LAST_NOTIFIED_UPDATE, value).apply()

    /** Self-signed-up user still on the placeholder name: the app asks "What's your name?". */
    val needsName: Boolean
        get() = prefs.getBoolean(KEY_NEEDS_NAME, false)

    var isSuperAdmin: Boolean
        get() = prefs.getBoolean(KEY_IS_SUPER_ADMIN, false)
        set(value) = prefs.edit().putBoolean(KEY_IS_SUPER_ADMIN, value).apply()

    /** Client-side only: a super admin chose "View as Coach". The app then behaves exactly like a coach. */
    var viewAsCoach: Boolean
        get() = prefs.getBoolean(KEY_VIEW_AS_COACH, false)
        set(value) = prefs.edit().putBoolean(KEY_VIEW_AS_COACH, value).apply()

    /** Display label for the non-admin role, from /settings/recorder-config ("Coach"). */
    var roleLabel: String
        get() = prefs.getString(KEY_ROLE_LABEL, DEFAULT_ROLE_LABEL)?.ifBlank { null } ?: DEFAULT_ROLE_LABEL
        set(value) = prefs.edit().putString(KEY_ROLE_LABEL, value).apply()

    /** Last good /settings/recorder-config response (raw JSON) so lists work offline. */
    var recorderConfigJson: String?
        get() = prefs.getString(KEY_RECORDER_CONFIG_JSON, null)
        set(value) = prefs.edit().putString(KEY_RECORDER_CONFIG_JSON, value).apply()

    val isLoggedIn: Boolean
        get() = !token.isNullOrBlank()

    /** True admin role from the server, ignoring the "view as Coach" switch. */
    val isRealAdmin: Boolean
        get() = userRole == "admin"

    /** Effective admin: what every screen uses. False while a super admin is viewing as Coach. */
    val isAdmin: Boolean
        get() = isRealAdmin && !viewAsCoach

    /** "Admin" or the configured coach label. */
    val displayRole: String
        get() = if (isAdmin) "Admin" else roleLabel

    fun saveUserSession(token: String, user: Map<String, Any?>) {
        prefs.edit()
            .putString(KEY_JWT_TOKEN, token)
            .putString(KEY_USER_ID, user["user_id"]?.toString() ?: user["id"]?.toString())
            .putString(KEY_USER_NAME, user["name"]?.toString())
            .putString(KEY_USER_PHONE, user["phone"]?.toString())
            .putString(KEY_USER_ROLE, user["role"]?.toString() ?: "staff")
            .putString(KEY_ORG_ID, user["org_id"]?.toString())
            .putString(KEY_ORG_NAME, user["org_name"]?.toString())
            .putBoolean(KEY_IS_SUPER_ADMIN, user["is_super_admin"] == true)
            .putBoolean(KEY_VIEW_AS_COACH, false)
            .putBoolean(KEY_NEEDS_NAME, user["needs_name"] == true)
            .apply()
    }

    /** Refresh profile fields from GET /auth/me without touching the token. */
    fun updateProfile(me: Map<String, Any?>) {
        val edit = prefs.edit()
        me["name"]?.toString()?.let { edit.putString(KEY_USER_NAME, it) }
        me["phone"]?.toString()?.let { edit.putString(KEY_USER_PHONE, it) }
        me["role"]?.toString()?.let { edit.putString(KEY_USER_ROLE, it) }
        me["org_id"]?.toString()?.let { edit.putString(KEY_ORG_ID, it) }
        me["org_name"]?.toString()?.let { edit.putString(KEY_ORG_NAME, it) }
        (me["is_super_admin"] as? Boolean)?.let {
            edit.putBoolean(KEY_IS_SUPER_ADMIN, it)
            if (!it) edit.putBoolean(KEY_VIEW_AS_COACH, false)
        }
        me["role_label"]?.toString()?.takeIf { it.isNotBlank() && it != "Admin" }?.let { edit.putString(KEY_ROLE_LABEL, it) }
        (me["needs_name"] as? Boolean)?.let { edit.putBoolean(KEY_NEEDS_NAME, it) }
        edit.apply()
    }

    // ── Calls (14 Oct): sync of the phone's own call recordings ────────────────
    /** "Sync my call recordings" switch. Off until the person turns it on. */
    var callSyncOn: Boolean
        get() = prefs.getBoolean("call_sync_on", false)
        set(value) = prefs.edit().putBoolean("call_sync_on", value).apply()

    /** Only calls recorded after this moment are synced (the moment sync was turned on), never the phone's old calls. */
    var callSyncSinceMs: Long
        get() = prefs.getLong("call_sync_since", 0L)
        set(value) = prefs.edit().putLong("call_sync_since", value).apply()

    var lastCallSyncMs: Long
        get() = prefs.getLong("call_sync_last", 0L)
        set(value) = prefs.edit().putLong("call_sync_last", value).apply()

    /** Phone files already uploaded (key = media id + size + modified time), per signed-in user. */
    fun syncedCallKeys(): Set<String> = prefs.getStringSet("call_synced_${userId}", emptySet()) ?: emptySet()

    fun addSyncedCallKey(key: String) {
        val all = syncedCallKeys().toMutableSet().apply { add(key) }
        prefs.edit().putStringSet("call_synced_${userId}", all).apply()
    }

    fun clear() {
        val currentServer = serverUrl
        prefs.edit().clear().putString(KEY_SERVER_URL, currentServer).apply()
    }
}
