package com.isf.recorder.ui.components

import android.app.DownloadManager
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.Environment
import android.widget.Toast
import java.time.Instant
import java.time.LocalDate
import java.time.OffsetDateTime
import java.time.ZoneId
import java.time.format.DateTimeFormatter
import java.util.Locale

/** India time — "today", chips and dates are IST calendar days (contract §4.6). */
val IST: ZoneId = ZoneId.of("Asia/Kolkata")

private val dayMonthYear = DateTimeFormatter.ofPattern("d MMM yyyy", Locale.ENGLISH)
private val dayMonthYearTime = DateTimeFormatter.ofPattern("d MMM yyyy, h:mm a", Locale.ENGLISH)
private val dayMonthTime = DateTimeFormatter.ofPattern("d MMM, h:mm a", Locale.ENGLISH)
private val headerDate = DateTimeFormatter.ofPattern("EEE, d MMM yyyy", Locale.ENGLISH)
private val shortDateTime = DateTimeFormatter.ofPattern("d MMM, h:mm a", Locale.ENGLISH)

fun todayHeaderDate(): String = LocalDate.now(IST).format(headerDate)

fun LocalDate.isoDay(): String = this.toString() // YYYY-MM-DD

/** Parse an ISO-8601 server timestamp (UTC or with offset) to IST; null when missing/unparseable. */
fun parseServerTime(raw: Any?): java.time.ZonedDateTime? {
    val s = raw?.toString()?.trim()?.takeIf { it.isNotEmpty() && it != "null" } ?: return null
    return try {
        OffsetDateTime.parse(s).atZoneSameInstant(IST)
    } catch (e: Exception) {
        try {
            Instant.parse(s).atZone(IST)
        } catch (e2: Exception) {
            try {
                java.time.LocalDateTime.parse(s).atZone(java.time.ZoneOffset.UTC).withZoneSameInstant(IST)
            } catch (e3: Exception) {
                null
            }
        }
    }
}

/** "6 Oct 2026" for a recording map (server date_formatted first). */
fun recordingDate(rec: Map<String, Any?>): String {
    // Date AND start time in India time, e.g. "6 Oct 2026, 9:13 AM" (admins asked to see when it started)
    // year only for other years, so the start time always fits on the card's first line
    parseServerTime(rec["started_at"] ?: rec["created_at"])?.let {
        return it.format(if (it.year == LocalDate.now(IST).year) dayMonthTime else dayMonthYearTime)
    }
    rec["started_at_formatted"]?.toString()?.takeIf { it.isNotBlank() }?.let { return it }
    return rec["date_formatted"]?.toString().orEmpty()
}

/** Short relative-ish date for list rows: "Today, 4:10 PM" / "6 Oct, 4:10 PM". */
fun shortWhen(raw: Any?): String {
    val t = parseServerTime(raw) ?: return ""
    val today = LocalDate.now(IST)
    return when (t.toLocalDate()) {
        today -> "Today, " + t.format(DateTimeFormatter.ofPattern("h:mm a", Locale.ENGLISH))
        today.minusDays(1) -> "Yesterday, " + t.format(DateTimeFormatter.ofPattern("h:mm a", Locale.ENGLISH))
        else -> t.format(shortDateTime)
    }
}

fun modeLabel(mode: Any?): String? = when (mode?.toString()?.lowercase()) {
    "schedule" -> "Schedule"
    "meeting" -> "Meeting"
    else -> null
}

/** Saved voice seconds if the server has them, else the legacy duration. */
fun recordingSeconds(rec: Map<String, Any?>): Int =
    (rec["voice_seconds"] as? Number)?.toInt()?.takeIf { it > 0 }
        ?: (rec["duration_seconds"] as? Number)?.toInt() ?: 0

/** "34 min", "1 h 05 min", "45 sec". */
fun humanDuration(totalSec: Int): String = when {
    totalSec <= 0 -> "0 min"
    totalSec < 60 -> "$totalSec sec"
    totalSec < 3600 -> "${totalSec / 60} min"
    else -> String.format(Locale.ENGLISH, "%d h %02d min", totalSec / 3600, (totalSec % 3600) / 60)
}

fun recordingDurationText(rec: Map<String, Any?>): String =
    rec["duration_formatted"]?.toString()?.takeIf { it.isNotBlank() } ?: humanDuration(recordingSeconds(rec))

/** HH:MM:SS for the big timer. */
fun clockHms(totalSec: Int): String =
    String.format(Locale.ENGLISH, "%02d:%02d:%02d", totalSec / 3600, (totalSec % 3600) / 60, totalSec % 60)

/** mm:ss or h:mm:ss for player times. */
fun clockShort(ms: Long): String {
    val total = (ms / 1000).coerceAtLeast(0)
    val h = total / 3600
    val m = (total % 3600) / 60
    val s = total % 60
    return if (h > 0) String.format(Locale.ENGLISH, "%d:%02d:%02d", h, m, s) else String.format(Locale.ENGLISH, "%02d:%02d", m, s)
}

fun cleanTitle(rec: Map<String, Any?>): String =
    (rec["title"]?.toString() ?: "Untitled recording").replace("\\r\\n", " ").replace("\\n", " ").trim()
        .ifBlank { "Untitled recording" }

fun initialsOf(name: String?): String {
    val parts = name.orEmpty().trim().split(Regex("\\s+")).filter { it.isNotBlank() }
    return when {
        parts.isEmpty() -> "?"
        parts.size == 1 -> parts[0].take(2).uppercase()
        else -> (parts[0].take(1) + parts[1].take(1)).uppercase()
    }
}

// ── Clipboard / share / download ────────────────────────────────────────────────

fun copyToClipboard(context: Context, label: String, text: String, toast: String = "Copied") {
    val clipboard = context.getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
    clipboard.setPrimaryClip(ClipData.newPlainText(label, text))
    Toast.makeText(context, toast, Toast.LENGTH_SHORT).show()
}

/**
 * The ONE place report text leaves the app: Copy and Share send exactly the same full text.
 * Share puts the text in EXTRA_TEXT and in ClipData (some apps read only the clip), and keeps
 * EXTRA_TITLE out (that is only the share-sheet preview title).
 */
/** Longer than this (only very long transcripts), Share sends a .txt file instead of a message. */
private const val SHARE_AS_FILE_CHARS = 60_000   // WhatsApp's own message limit is ~65,536: reports always go as text

/**
 * The AI writes reports in Markdown (**bold**, *italic*, "*   item" bullets, # headings, ---).
 * Messaging apps show those marks literally, so Copy/Share convert to WhatsApp-style text:
 * bold = *x*, italic = _x_, bullets = "• ", headings and whole-line *x* = bold, --- = a line.
 */
fun markdownToChatText(md: String): String {
    val bullet = Regex("""^([-*+•])\s+(.*)$""")
    val rule = Regex("""^(-{3,}|\*{3,}|_{3,})$""")
    fun inline(t: String): String {
        var x = t.replace(Regex("""(?<!\*)\*(?![*\s])(.+?)(?<![*\s])\*(?!\*)"""), "_$1_")   // *italic* -> _italic_
        x = x.replace(Regex("""\*\*(.+?)\*\*"""), "*$1*")                                // **bold** -> *bold*
        x = x.replace(Regex("""__(.+?)__"""), "*$1*")
        return x
    }
    return md.replace("\r\n", "\n").split("\n").joinToString("\n") { raw ->
        val line = raw.trimEnd()
        val t = line.trimStart()
        val indent = line.length - t.length
        when {
            t.isEmpty() -> ""
            rule.matches(t) -> "──────────"
            t.startsWith("#") -> "*" + t.trimStart('#').trim().replace("**", "") + "*"
            t.length > 2 && t.startsWith("*") && t.endsWith("*") && !t.startsWith("**") && !t.startsWith("* ") ->
                "*" + t.trim('*').trim() + "*"                                          // *1. Heading* -> bold
            bullet.matches(t) -> "  ".repeat((indent / 4).coerceAtMost(2)) + "• " + inline(bullet.find(t)!!.groupValues[2].trim())
            else -> inline(t)
        }
    }.replace(Regex("""\n{3,}"""), "\n\n").trim()
}

fun copyOrShareText(context: Context, subject: String, text: String, share: Boolean, toast: String = "Report copied") {
    val text = markdownToChatText(text)
    if (share) {
        shareText(context, subject, text)
    } else {
        val note = if (text.length > SHARE_AS_FILE_CHARS)
            "$toast (${text.length} characters). Some apps cut long pastes — use Share to send it as a file." else toast
        copyToClipboard(context, subject, text, note)
    }
}

fun shareText(context: Context, subject: String, text: String) {
    try {
        val send = Intent(Intent.ACTION_SEND).apply {
            type = "text/plain"
            putExtra(Intent.EXTRA_SUBJECT, subject)
        }
        if (text.length > SHARE_AS_FILE_CHARS) {
            val dir = java.io.File(context.cacheDir, "shared").apply { mkdirs() }
            dir.listFiles()?.forEach { it.delete() }   // keep only the latest shared file
            val safe = subject.replace(Regex("[^\\p{L}\\p{N} _.-]"), "_").trim().take(60).ifBlank { "Jomi" }
            val file = java.io.File(dir, "$safe.txt").apply { writeText(text, Charsets.UTF_8) }
            val uri = androidx.core.content.FileProvider.getUriForFile(context, context.packageName + ".fileprovider", file)
            send.putExtra(Intent.EXTRA_STREAM, uri)
            send.putExtra(Intent.EXTRA_TEXT, "$subject (full text attached)")
            send.clipData = ClipData.newRawUri(subject, uri)
            send.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
        } else {
            send.putExtra(Intent.EXTRA_TEXT, text)
            send.clipData = ClipData.newPlainText(subject, text)
        }
        context.startActivity(Intent.createChooser(send, "Share via").addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
    } catch (e: Exception) {
        Toast.makeText(context, "Could not share: ${e.message}", Toast.LENGTH_SHORT).show()
    }
}

/**
 * Downloads the authenticated audio with DownloadManager, sending the Authorization header
 * (never a ?token= URL). Android 10+ saves to public Downloads; older phones to the app's
 * own Downloads folder (no storage permission needed).
 */
fun downloadAuthenticated(context: Context, url: String, token: String?, fileName: String) {
    try {
        val cleanName = fileName.replace(Regex("[^a-zA-Z0-9_.-]"), "_").take(80)
        val request = DownloadManager.Request(Uri.parse(url))
            .setTitle(cleanName)
            .setDescription("Downloading recording audio")
            .setMimeType(if (cleanName.endsWith(".wav", true)) "audio/wav" else "audio/mp4")
            .setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED)
            .setAllowedOverMetered(true)
            .setAllowedOverRoaming(true)
        if (!token.isNullOrBlank()) request.addRequestHeader("Authorization", "Bearer $token")
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            request.setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS, cleanName)
        } else {
            request.setDestinationInExternalFilesDir(context, Environment.DIRECTORY_DOWNLOADS, cleanName)
        }
        val dm = context.getSystemService(Context.DOWNLOAD_SERVICE) as DownloadManager
        dm.enqueue(request)
        Toast.makeText(context, "Downloading $cleanName…", Toast.LENGTH_SHORT).show()
    } catch (e: Exception) {
        Toast.makeText(context, "Download failed: ${e.message}", Toast.LENGTH_LONG).show()
    }
}

private fun namesOf(raw: Any?): List<String> = (raw as? List<*>)?.mapNotNull { item ->
    when (item) {
        is Map<*, *> -> item["name"]?.toString()?.takeIf { it.isNotBlank() }
        is String -> item.takeIf { it.isNotBlank() }
        else -> null
    }
} ?: emptyList()

/** "Mohor Boys, Somwar Peth · Rizwana, Archana" (schedule) or the participants (meeting). */
/**
 * Who / where / when of a recording, shown above every report and put at the top of Copy/Share:
 * date + start time, Balgruh, recorded by, assistant coaches (or meeting participants).
 * Taken from the recording itself, never from the AI text.
 */
fun reportDetailsLines(rec: Map<String, Any?>?): List<String> {
    if (rec == null) return emptyList()
    fun clean(v: Any?) = v?.toString()?.trim()?.takeIf { it.isNotBlank() && it != "null" }
    val lines = mutableListOf<String>()
    listOfNotNull(recordingDate(rec).takeIf { it.isNotBlank() }, modeLabel(rec["mode"]))
        .joinToString(" · ").takeIf { it.isNotBlank() }?.let { lines += "📅 $it" }
    clean(rec["balgruh_name"])?.let { lines += "🏠 Balgruh: $it" }
    (clean(rec["user_name"]) ?: clean(rec["recorded_by_name"]) ?: clean(rec["recorded_by"]))?.let { lines += "🎙️ Recorded by: $it" }
    if (rec["mode"]?.toString() == "meeting") {
        namesOf(rec["participants"]).takeIf { it.isNotEmpty() }?.let { lines += "👥 Participants: " + it.joinToString(", ") }
    } else {
        val coaches = namesOf(rec["assistant_coaches"]).ifEmpty { listOfNotNull(clean(rec["assistant_coach"])) }
        if (coaches.isNotEmpty()) lines += "👥 Assistant coaches: " + coaches.joinToString(", ")
    }
    return lines
}

fun recordingPeopleLine(rec: Map<String, Any?>): String? {
    val coaches = namesOf(rec["assistant_coaches"]).ifEmpty {
        listOfNotNull(rec["assistant_coach"]?.toString()?.takeIf { it.isNotBlank() && it != "null" })
    }
    val meeting = rec["mode"]?.toString() == "meeting"
    val people = if (meeting) namesOf(rec["participants"]) else coaches
    val parts = if (meeting) {
        listOf(people.joinToString(", "))
    } else {
        listOf(rec["balgruh_name"]?.toString()?.takeIf { it != "null" }.orEmpty(), coaches.joinToString(", "))
    }
    val line = parts.filter { it.isNotBlank() }.joinToString(" · ").ifBlank { null }
    val title = rec["title"]?.toString()?.trim().orEmpty()
    // Schedule titles are already "<Balgruh> · <coaches>": do not repeat them under the title. An edited title
    // ("Shri Ramkrushna VS - Archana, Sanket") that still names every coach / participant counts as the same.
    val titleNamesEveryone = people.isNotEmpty() && people.all { title.contains(it.trim(), ignoreCase = true) }
    return line?.takeUnless { title.equals(it, ignoreCase = true) || titleNamesEveryone }
}

/** True when the server says the audio went to the old 7-day cleanup (audio_deleted_at set). */
fun audioWasCleanedUp(rec: Map<String, Any?>?): Boolean =
    rec?.get("audio_deleted_at")?.toString()?.let { it.isNotBlank() && it != "null" } == true

/** Wording for a recording whose audio file is not on the server (HTTP 410 / has_audio=false). */
fun audioUnavailableMessage(cleanedUp: Boolean): String =
    if (cleanedUp) "Audio file is not available for this recording. Older recordings lost their audio to the old 7-day cleanup."
    else "Audio file is not available for this recording."


/** "45 s", "1 min 20 s", "1 h 5 min" — for skipped-silence markers. */
fun gapDurationText(seconds: Double): String {
    val t = seconds.toInt().coerceAtLeast(0)
    return when {
        t < 60 -> "$t s"
        t < 3600 -> if (t % 60 == 0) "${t / 60} min" else "${t / 60} min ${t % 60} s"
        else -> "${t / 3600} h ${(t % 3600) / 60} min"
    }
}

/** "Silence skipped: 3× · 4 min" when the phone skipped silence (v2.1), else null. */
fun silenceSkippedLine(rec: Map<String, Any?>?): String? {
    val n = (rec?.get("silence_skipped_count") as? Number)?.toInt() ?: 0
    if (n <= 0) return null
    val secs = (rec?.get("silence_skipped_seconds") as? Number)?.toInt() ?: 0
    return "Silence skipped: $n× · ${humanDuration(secs)}"
}

/** Skipped-silence markers of a recording: (at_voice_sec, seconds), sorted. */
fun silenceGapsOf(rec: Map<String, Any?>?): List<Pair<Double, Double>> =
    ((rec?.get("silence_gaps") as? List<*>) ?: emptyList<Any?>()).mapNotNull { g ->
        val m = g as? Map<*, *> ?: return@mapNotNull null
        val at = (m["at_voice_sec"] as? Number)?.toDouble() ?: return@mapNotNull null
        val secs = (m["seconds"] as? Number)?.toDouble() ?: return@mapNotNull null
        at to secs
    }.sortedBy { it.first }

/** "Mohor Boys · Rizwana, Archana" (schedule) or the participants (meeting), never deduplicated against the title. */
fun recordingContextLine(rec: Map<String, Any?>): String? {
    fun names(raw: Any?) = (raw as? List<*>)?.mapNotNull { (it as? Map<*, *>)?.get("name")?.toString() ?: (it as? String) }
        ?.filter { it.isNotBlank() } ?: emptyList()
    return if (rec["mode"]?.toString() == "meeting") names(rec["participants"]).joinToString(", ").ifBlank { null }
    else {
        val coaches = names(rec["assistant_coaches"]).ifEmpty {
            listOfNotNull(rec["assistant_coach"]?.toString()?.takeIf { it.isNotBlank() && it != "null" })
        }
        listOfNotNull(rec["balgruh_name"]?.toString()?.takeIf { it.isNotBlank() && it != "null" }, coaches.joinToString(", ").ifBlank { null })
            .joinToString(" · ").ifBlank { null }
    }
}

/** "SPEAKER_01" -> "Speaker 1"; real names are kept. */
fun speakerDisplayName(raw: Any?): String {
    val s = raw?.toString()?.trim().orEmpty()
    val m = Regex("^SPEAKER_?0*(\\d+)$", RegexOption.IGNORE_CASE).find(s)
    return when {
        s.isEmpty() -> "Speaker"
        m != null -> "Speaker ${m.groupValues[1].toInt().let { if (it == 0) 1 else it }}"
        else -> s
    }
}

private fun mmss(sec: Double): String {
    val t = sec.toInt().coerceAtLeast(0)
    return if (t >= 3600) String.format(Locale.ENGLISH, "%d:%02d:%02d", t / 3600, (t % 3600) / 60, t % 60)
    else String.format(Locale.ENGLISH, "%02d:%02d", t / 60, t % 60)
}

/**
 * Full transcript text for Copy / Share: header (title, date, Balgruh/coaches or participants) then
 * "[MM:SS] Speaker: text" lines with "[silence 1 min 20 s skipped]" at each skipped-silence marker.
 */
fun buildTranscriptText(rec: Map<String, Any?>, segments: List<Map<String, Any?>>,
                        gaps: List<Pair<Double, Double>> = silenceGapsOf(rec)): String = buildString {
    append(cleanTitle(rec))
    recordingDate(rec).takeIf { it.isNotBlank() }?.let { append("\n").append(it) }
    recordingContextLine(rec)?.let { append("\n").append(it) }
    append("\n\n")
    var g = 0
    for (seg in segments) {
        val start = (seg["start_time"] as? Number)?.toDouble() ?: 0.0
        while (g < gaps.size && gaps[g].first <= start) {
            append("[silence ").append(gapDurationText(gaps[g].second)).append(" skipped]\n"); g++
        }
        append("[").append(mmss(start)).append("] ").append(speakerDisplayName(seg["speaker"])).append(": ")
            .append(seg["text"]?.toString()?.trim().orEmpty()).append("\n")
    }
    while (g < gaps.size) { append("[silence ").append(gapDurationText(gaps[g].second)).append(" skipped]\n"); g++ }
}.trimEnd()
