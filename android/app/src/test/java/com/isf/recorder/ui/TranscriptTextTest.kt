package com.isf.recorder.ui

import com.isf.recorder.ui.components.buildTranscriptText
import com.isf.recorder.ui.components.gapDurationText
import com.isf.recorder.ui.components.silenceSkippedLine
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class TranscriptTextTest {
    private val rec = mapOf<String, Any?>(
        "title" to "Mohor Boys · Rizwana", "date_formatted" to "12 Oct 2026", "mode" to "schedule",
        "balgruh_name" to "Mohor Boys", "assistant_coaches" to listOf(mapOf("name" to "Rizwana")),
        "silence_gaps" to listOf(mapOf("at_voice_sec" to 10.0, "seconds" to 80.0), mapOf("at_voice_sec" to 99.0, "seconds" to 5.0)),
        "silence_skipped_count" to 2.0, "silence_skipped_seconds" to 85.0)
    private val segs = listOf(
        mapOf<String, Any?>("start_time" to 2.0, "speaker" to "SPEAKER_00", "text" to "Namaste"),
        mapOf<String, Any?>("start_time" to 12.5, "speaker" to "SPEAKER_02", "text" to " Chalo shuru karte hain "))

    @Test
    fun transcriptTextHasHeaderLinesAndSilenceMarkers() {
        assertEquals(
            "Mohor Boys · Rizwana\n12 Oct 2026\nMohor Boys · Rizwana\n\n" +
                "[00:02] Speaker 1: Namaste\n[silence 1 min 20 s skipped]\n[00:12] Speaker 2: Chalo shuru karte hain\n" +
                "[silence 5 s skipped]",
            buildTranscriptText(rec, segs))
    }

    @Test
    fun silenceLineAndDurations() {
        assertEquals("Silence skipped: 2× · 1 min", silenceSkippedLine(rec))
        assertNull(silenceSkippedLine(mapOf("silence_skipped_count" to 0.0)))
        assertNull(silenceSkippedLine(emptyMap()))
        assertEquals("45 s", gapDurationText(45.0)); assertEquals("2 min", gapDurationText(120.0))
        assertEquals("1 h 5 min", gapDurationText(3900.0))
    }
}
