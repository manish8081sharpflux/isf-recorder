package com.isf.recorder.sync

import java.time.LocalDateTime
import java.time.ZoneId
import java.time.format.DateTimeFormatter

/**
 * Reads contact / number / call time from a phone call-recorder file name (ported from GU Bandhu's
 * SamsungFilenameParser.js). Pure Kotlin, unit-tested. Unknown patterns fall back to the file time.
 *
 *   Samsung:  "Call recording Bhavnesh Desai Sir_261011_135446"      (yyMMdd_HHmmss)
 *             "Call recording Rizwana 2026-10-11 13.54.46"
 *   Generic:  "Rizwana_2026-10-11_13-54-46", "20261011135446_Rizwana", "Call with Rizwana 2026-10-11 13-54-46"
 *   Xiaomi:   "Rizwana(+919876543210)_20261011135446", "(9876543210)_20261011_135446"
 */
object CallFileParser {
    data class Parsed(val contactName: String?, val contactPhone: String?, val callTimeMs: Long?)

    private val IST: ZoneId = ZoneId.of("Asia/Kolkata")
    private val PHONE_ONLY = Regex("""^\+?\d{10,}$""")
    private val PREFIX = Regex("""^(call recording|call_recording|call|record|rec)[\s_-]+""", RegexOption.IGNORE_CASE)

    /** Folders that hold call recordings only (never generic voice notes / screen recordings). */
    val CALL_FOLDER_HINTS = listOf(
        "recordings/call", "/call/", "call recording", "call_rec", "call_recording", "callrecord", "call records",
        "acrcalls", "cube acr", "cubeacr", "phonerecord", "sound_recorder/call", "miui/sound_recorder/call"
    )

    fun isCallFolder(relativePathOrData: String?): Boolean {
        val p = (relativePathOrData ?: "").lowercase().replace('\\', '/')
        return CALL_FOLDER_HINTS.any { p.contains(it) }
    }

    fun parse(fileName: String): Parsed {
        val base = fileName.substringBeforeLast('.').trim()
        fun time(pattern: String, s: String): Long? = try {
            LocalDateTime.parse(s, DateTimeFormatter.ofPattern(pattern)).atZone(IST).toInstant().toEpochMilli()
        } catch (_: Exception) { null }
        fun who(raw: String): Pair<String?, String?> {
            val c = raw.replace(PREFIX, "").trim().trim('_', '-', ' ')
            val digits = c.replace(Regex("""[\s\-]"""), "")
            return if (PHONE_ONLY.matches(digits)) null to digits else c.ifBlank { null } to null
        }

        Regex("""^(.+?)_(\d{6})_(\d{6})$""").find(base)?.let { m ->
            val (n, p) = who(m.groupValues[1]); return Parsed(n, p, time("yyMMddHHmmss", m.groupValues[2] + m.groupValues[3]))
        }
        Regex("""^(?:Call recording |Call with )(.+?) (\d{4}-\d{2}-\d{2})[ _](\d{2})[-:.](\d{2})[-:.](\d{2})$""", RegexOption.IGNORE_CASE)
            .find(base)?.let { m ->
                val (n, p) = who(m.groupValues[1])
                val g = m.groupValues
                return Parsed(n, p, time("yyyy-MM-dd HHmmss", "${g[2]} ${g[3]}${g[4]}${g[5]}"))
            }
        Regex("""^(.+?)_(\d{4}-\d{2}-\d{2})_(\d{2})-(\d{2})-(\d{2})$""").find(base)?.let { m ->
            val (n, p) = who(m.groupValues[1]); val g = m.groupValues
            return Parsed(n, p, time("yyyy-MM-dd HHmmss", "${g[2]} ${g[3]}${g[4]}${g[5]}"))
        }
        Regex("""^(.*?)\((\+?\d+)\)[_ ]?(\d{14}|\d{8}_\d{6})$""").find(base)?.let { m ->
            val name = m.groupValues[1].replace(PREFIX, "").trim().trim('_', '-').ifBlank { null }
            return Parsed(name, m.groupValues[2], time("yyyyMMddHHmmss", m.groupValues[3].replace("_", "")))
        }
        Regex("""^(\d{14})_(.+)$""").find(base)?.let { m ->
            val (n, p) = who(m.groupValues[2]); return Parsed(n, p, time("yyyyMMddHHmmss", m.groupValues[1]))
        }
        // Unknown layout: best effort on the name; a 10-digit mobile anywhere becomes the number
        val phone = Regex("""(?:\+?91|0)?([6-9]\d{9})""").find(base)?.groupValues?.get(1)
        val name = base.replace(PREFIX, "").replace(Regex("""[_-]?\d{6,}.*$"""), "").trim().trim('_', '-')
            .ifBlank { null }?.takeUnless { phone != null && it.filter(Char::isDigit).length >= 10 }
        return Parsed(name, phone, null)
    }
}
