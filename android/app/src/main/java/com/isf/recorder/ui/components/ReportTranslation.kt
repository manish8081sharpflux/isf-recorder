package com.isf.recorder.ui.components

import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.isf.recorder.data.api.ISFApiClient
import com.isf.recorder.ui.theme.*

/**
 * Report translation (13 Oct): English | हिंदी (Marathi not needed). A translation is made once by the server and
 * saved; it is fetched again only when the English report changed. Editing always happens in English.
 */
class ReportTranslation {
    var lang by mutableStateOf(EN)
    val cache = mutableStateMapOf<String, String>()
    var loading by mutableStateOf(false)
    var error by mutableStateOf<String?>(null)
    var attempt by mutableStateOf(0)

    fun retry() { error = null; attempt++ }

    val isEnglish: Boolean get() = lang == EN

    /** The text to show / copy / share for [english]: the translation when one is chosen and ready. */
    fun shown(english: String): String = if (isEnglish) english else cache[lang] ?: ""

    companion object {
        const val EN = "en"
        val LANGUAGES = listOf(EN to "English", "hi" to "हिंदी")
    }
}

@Composable
fun rememberReportTranslation(api: ISFApiClient, recordingId: String, english: String): ReportTranslation {
    val t = remember(recordingId) { ReportTranslation() }
    // A changed English report makes every saved translation stale.
    var source by remember(recordingId) { mutableStateOf(english) }
    if (english != source) {
        source = english
        t.cache.clear()
    }
    LaunchedEffect(t.lang, source, t.attempt) {
        val lang = t.lang
        if (lang == ReportTranslation.EN || t.cache[lang] != null || source.isBlank()) return@LaunchedEffect
        t.loading = true
        t.error = null
        api.translateReport(recordingId, lang)
            .onSuccess { t.cache[lang] = it }
            .onFailure { t.error = it.message ?: "Could not translate" }
        t.loading = false
    }
    return t
}

/** The language buttons shown above a report. */
@Composable
fun ReportLanguageBar(t: ReportTranslation, modifier: Modifier = Modifier) {
    Row(modifier = modifier, horizontalArrangement = Arrangement.spacedBy(8.dp), verticalAlignment = Alignment.CenterVertically) {
        Text("🌐", fontSize = 16.sp)
        ReportTranslation.LANGUAGES.forEach { (code, label) ->
            val on = t.lang == code
            Surface(
                onClick = { t.lang = code },
                shape = RoundedCornerShape(16.dp),
                color = if (on) JomiGreen else Color.White,
                border = BorderStroke(1.dp, if (on) JomiGreen else JomiBorder)
            ) {
                Text(label, color = if (on) Color.White else MaterialTheme.colorScheme.onBackground,
                    fontWeight = if (on) FontWeight.Bold else FontWeight.Normal, fontSize = 14.sp,
                    modifier = Modifier.padding(horizontal = 14.dp, vertical = 6.dp))
            }
        }
        if (t.loading) CircularProgressIndicator(Modifier.size(16.dp), color = JomiGreen, strokeWidth = 2.dp)
    }
}

/** Shown instead of the report body while a translation loads or when it failed. */
@Composable
fun TranslationStatus(t: ReportTranslation) {
    when {
        t.loading -> Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.padding(vertical = 16.dp)) {
            CircularProgressIndicator(Modifier.size(18.dp), color = JomiGreen, strokeWidth = 2.dp)
            Spacer(Modifier.width(10.dp))
            Text("Translating…", color = JomiTextMuted, fontSize = 14.sp)
        }
        t.error != null -> Column {
            Text(t.error ?: "", color = RecordRed, fontSize = 13.sp)
            TextButton(onClick = { t.retry() }) { Text("Try again", color = JomiTeal) }
        }
    }
}
