package com.isf.recorder.ui.components

import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.tooling.preview.Preview
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.isf.recorder.ui.theme.ISFRecorderTheme

/**
 * One compact, same-height row per AI model (Settings → AI model, intelligence model, upload picker):
 * radio · short name (one line) + optional small chip · muted "provider · price" line (one line).
 * The long description is behind ⓘ (tap to show), never in the row itself.
 */
@Composable
fun ModelOptionRow(
    model: Map<String, Any?>,
    selected: Boolean,
    onSelect: () -> Unit,
    accent: Color,
    enabled: Boolean = true,
    showPrice: Boolean = true
) {
    val id = model["id"]?.toString().orEmpty()
    val name = model["short_name"]?.toString()?.takeIf { it.isNotBlank() } ?: model["name"]?.toString() ?: id
    val chip = model["chip"]?.toString()?.takeIf { it.isNotBlank() }
    val provider = model["provider"]?.toString().orEmpty()
    val price = model["price_short"]?.toString()?.takeIf { it.isNotBlank() && showPrice }
    val description = model["description"]?.toString().orEmpty()
    var info by remember(id) { mutableStateOf(false) }
    val colors = MaterialTheme.colorScheme

    Surface(
        shape = RoundedCornerShape(10.dp),
        color = if (selected) accent.copy(alpha = 0.12f) else colors.surfaceVariant,
        border = BorderStroke(if (selected) 1.5.dp else 1.dp, if (selected) accent else colors.outline.copy(alpha = 0.5f)),
        modifier = Modifier.fillMaxWidth().clickable(enabled = enabled, onClick = onSelect)
    ) {
        Column(Modifier.padding(horizontal = 4.dp, vertical = 2.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                RadioButton(selected = selected, onClick = onSelect, enabled = enabled,
                    colors = RadioButtonDefaults.colors(selectedColor = accent))
                Column(Modifier.weight(1f)) {
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Text(name, color = colors.onBackground, fontSize = 14.sp,
                            fontWeight = if (selected) FontWeight.Bold else FontWeight.SemiBold,
                            maxLines = 1, overflow = TextOverflow.Ellipsis, modifier = Modifier.weight(1f, fill = false))
                        if (chip != null) {
                            Spacer(Modifier.width(6.dp))
                            Surface(shape = RoundedCornerShape(6.dp), color = accent.copy(alpha = 0.15f)) {
                                Text(chip, color = accent, fontSize = 10.sp, fontWeight = FontWeight.SemiBold, maxLines = 1,
                                    modifier = Modifier.padding(horizontal = 5.dp, vertical = 1.dp))
                            }
                        }
                    }
                    Text(listOfNotNull(provider.takeIf { it.isNotBlank() }, price).joinToString(" · "),
                        color = colors.onSurfaceVariant, fontSize = 11.sp, maxLines = 1, overflow = TextOverflow.Ellipsis)
                }
                if (description.isNotBlank()) {
                    TextButton(onClick = { info = !info }, contentPadding = PaddingValues(0.dp), modifier = Modifier.size(36.dp)) {
                        Text("ⓘ", color = if (info) accent else colors.onSurfaceVariant, fontSize = 16.sp)
                    }
                }
            }
            if (info) {
                Text(description, color = colors.onSurfaceVariant, fontSize = 12.sp,
                    modifier = Modifier.padding(start = 48.dp, end = 8.dp, bottom = 8.dp))
            }
        }
    }
}

private val previewModels = listOf(
    mapOf("id" to "gemini-2.5-flash", "short_name" to "Gemini 2.5 Flash", "provider" to "Google",
        "price_short" to "≈ \$0.12/h + output", "description" to "Current default."),
    mapOf("id" to "openai-gpt-4o-transcribe-diarize", "short_name" to "GPT-4o Diarize", "chip" to "Speakers",
        "provider" to "OpenAI", "price_short" to "\$2.5 in / \$10 out per 1M tokens",
        "description" to "Transcript with speaker labels and times. Reports stay on Gemini."),
    mapOf("id" to "openai-gpt-transcribe", "short_name" to "GPT Transcribe", "chip" to "No speakers",
        "provider" to "OpenAI", "price_short" to "\$0.27/h", "description" to "Accurate text, no speakers."),
    mapOf("id" to "assemblyai-universal-3-5-pro", "short_name" to "AssemblyAI U3.5 Pro", "provider" to "AssemblyAI",
        "price_short" to "\$0.23/h", "description" to "Best AssemblyAI model, supports Hindi/Marathi.")
)

@Preview(name = "Model picker 360dp x1.3", showBackground = true, widthDp = 360, fontScale = 1.3f)
@Composable
private fun ModelOptionRowPreview() {
    ISFRecorderTheme {
        Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
            previewModels.forEachIndexed { i, m ->
                ModelOptionRow(m, selected = i == 1, onSelect = {}, accent = Color(0xFF0F766E))
            }
        }
    }
}
