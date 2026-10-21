package com.isf.recorder.sync

import com.isf.recorder.sync.ReportStatusWorker.Decision
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class ReportStatusDecisionTest {
    private fun item(status: String?, report: String?, title: String? = "Mohor Boys · Daily schedule") =
        mapOf("id" to "r1", "status" to status, "report_status" to report, "title" to title)

    @Test
    fun reportReady() {
        assertEquals(Decision.Notify("Report ready: Mohor Boys · Daily schedule", "Tap to open the report"),
            ReportStatusWorker.decide(item("completed", "draft"), null, 60_000))
        assertTrue(ReportStatusWorker.decide(item("completed", "final"), null, 60_000) is Decision.Notify)
    }

    @Test
    fun transcriptionFailed() {
        val d = ReportStatusWorker.decide(item("error", null), null, 60_000) as Decision.Notify
        assertEquals("Transcription failed — tap to retry", d.title)
    }

    @Test
    fun keepsPollingWhileProcessing() {
        assertEquals(Decision.KeepPolling, ReportStatusWorker.decide(item("processing", null), null, 60_000))
        assertEquals(Decision.KeepPolling, ReportStatusWorker.decide(item("completed", "generating"), null, 60_000))
        assertEquals(Decision.KeepPolling, ReportStatusWorker.decide(null, "x", 60_000))
        // completed without a report row: wait for the grace period first
        assertEquals(Decision.KeepPolling, ReportStatusWorker.decide(item("completed", null), null, 60_000))
        assertTrue(ReportStatusWorker.decide(item("completed", null), null, ReportStatusWorker.NO_REPORT_GRACE_MS) is Decision.Notify)
    }

    @Test
    fun noSpeechAndGiveUp() {
        val d = ReportStatusWorker.decide(item("completed", null, "No speech detected"), null, 60_000) as Decision.Notify
        assertEquals("No speech detected", d.title)
        assertEquals(Decision.GiveUp, ReportStatusWorker.decide(item("processing", null), null, ReportStatusWorker.GIVE_UP_AFTER_MS))
    }

    @Test
    fun fallbackTitle() {
        val d = ReportStatusWorker.decide(item("completed", "draft", title = null), "Weekly review", 1) as Decision.Notify
        assertEquals("Report ready: Weekly review", d.title)
    }
}
