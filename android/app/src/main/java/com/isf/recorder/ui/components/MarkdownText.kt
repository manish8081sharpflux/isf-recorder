package com.isf.recorder.ui.components

import androidx.compose.material3.HorizontalDivider

import androidx.compose.foundation.layout.*
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.remember
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.text.SpanStyle
import androidx.compose.ui.text.buildAnnotatedString
import androidx.compose.ui.text.font.FontStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.withStyle
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.isf.recorder.ui.theme.JomiTeal

/** Inline **bold** and *italic* → AnnotatedString. */
fun inlineMarkdown(line: String): AnnotatedString = buildAnnotatedString {
    var i = 0
    while (i < line.length) {
        when {
            line.startsWith("**", i) -> {
                val end = line.indexOf("**", i + 2)
                if (end > i + 2) {
                    withStyle(SpanStyle(fontWeight = FontWeight.Bold)) { append(line.substring(i + 2, end)) }
                    i = end + 2
                } else {
                    append("**"); i += 2
                }
            }
            line[i] == '*' && i + 1 < line.length && line[i + 1] != ' ' -> {
                val end = line.indexOf('*', i + 1)
                if (end > i + 1) {
                    withStyle(SpanStyle(fontStyle = FontStyle.Italic)) { append(line.substring(i + 1, end)) }
                    i = end + 1
                } else {
                    append('*'); i++
                }
            }
            else -> {
                append(line[i]); i++
            }
        }
    }
}

/**
 * Minimal markdown for reports: # headings, dash/star bullets, numbered lines, **bold**, *italic*.
 */
@Composable
fun MarkdownText(markdown: String, modifier: Modifier = Modifier) {
    val lines = remember(markdown) { markdown.replace("\r\n", "\n").split("\n") }
    val onBg = MaterialTheme.colorScheme.onBackground
    Column(modifier = modifier, verticalArrangement = Arrangement.spacedBy(3.dp)) {
        lines.forEach { raw ->
            val line = raw.trimEnd()
            val trimmed = line.trimStart()
            when {
                trimmed.isEmpty() -> Spacer(Modifier.height(6.dp))
                // ---, ***, ___ : a thin divider instead of literal dashes
                Regex("""^(-{3,}|\*{3,}|_{3,})$""").matches(trimmed) -> HorizontalDivider(
                    modifier = Modifier.padding(vertical = 6.dp), color = onBg.copy(alpha = 0.15f)
                )
                trimmed.startsWith("#") -> {
                    val level = trimmed.takeWhile { it == '#' }.length
                    Text(
                        inlineMarkdown(trimmed.drop(level).trim()),
                        color = JomiTeal,
                        fontSize = when (level) { 1 -> 20.sp; 2 -> 17.sp; else -> 15.sp },
                        fontWeight = FontWeight.Bold,
                        modifier = Modifier.padding(top = 6.dp)
                    )
                }
                trimmed.startsWith("- ") || trimmed.startsWith("* ") || trimmed.startsWith("• ") || trimmed.startsWith("+ ") -> {
                    val indent = ((line.length - trimmed.length) / 2).coerceAtMost(3)
                    Row(modifier = Modifier.padding(start = (8 + indent * 12).dp)) {
                        Text("•  ", color = onBg, fontSize = 14.sp)
                        Text(inlineMarkdown(trimmed.drop(2).trimStart()), color = onBg, fontSize = 14.sp, lineHeight = 20.sp)
                    }
                }
                else -> Text(inlineMarkdown(trimmed), color = onBg, fontSize = 14.sp, lineHeight = 20.sp)
            }
        }
    }
}
