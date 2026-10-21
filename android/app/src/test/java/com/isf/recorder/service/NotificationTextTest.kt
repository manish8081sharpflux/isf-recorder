package com.isf.recorder.service

import com.isf.recorder.data.model.RecordingStatus
import com.isf.recorder.data.model.sessionDisplayTitle
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.util.TimeZone

class NotificationTextTest {
    private val ist = TimeZone.getTimeZone("Asia/Kolkata")

    @Test
    fun foregroundTextPerState() {
        val rec = RecorderNotifications.foregroundContent(RecordingStatus.RECORDING, true, 75, false, null)
        assertEquals("Recording", rec.title)
        assertEquals("01:15 saved", rec.text)
        assertTrue(rec.showPause && rec.showStop && !rec.showResume)

        val listening = RecorderNotifications.foregroundContent(RecordingStatus.RECORDING, false, 3725, false, null)
        assertEquals("Listening — silence not saved", listening.title)
        assertEquals("1:02:05 saved", listening.text)

        val paused = RecorderNotifications.foregroundContent(RecordingStatus.PAUSED_USER, false, 0, false, null)
        assertEquals("Paused", paused.title)
        assertTrue(paused.showResume && paused.showStop && !paused.showPause)

        val call = RecorderNotifications.foregroundContent(RecordingStatus.PAUSED_CONFLICT, false, 0, true, null)
        assertTrue(call.title.contains("call"))
        assertTrue(call.showResume && call.showStop)

        val saving = RecorderNotifications.foregroundContent(RecordingStatus.FINALIZING, false, 10, false, null)
        assertFalse(saving.showPause || saving.showResume || saving.showStop)
    }

    @Test
    fun interruptedAndReminderTexts() {
        // 2026-10-07 04:30 UTC = 10:00 IST
        assertEquals("Your recording from 10:00 was interrupted and has been saved",
            RecorderNotifications.interruptedText(1_791_347_400_000L, ist))
        assertEquals("Still recording · 2 h", RecorderNotifications.reminderText(2))
    }

    @Test
    fun heartbeatStates() {
        assertEquals("recording", AudioRecordingService.heartbeatState(RecordingStatus.RECORDING, true))
        assertEquals("listening", AudioRecordingService.heartbeatState(RecordingStatus.RECORDING, false))
        assertEquals("paused", AudioRecordingService.heartbeatState(RecordingStatus.PAUSED_USER, false))
        assertEquals("paused", AudioRecordingService.heartbeatState(RecordingStatus.PAUSED_CONFLICT, true))
    }

    @Test
    fun displayTitleRule() {
        assertEquals("Mohor Boys · Daily schedule", sessionDisplayTitle("schedule", "Mohor Boys", null))
        assertEquals("Mohor Boys, Somwar Peth · Rizwana, Archana",
            sessionDisplayTitle("schedule", "Mohor Boys, Somwar Peth", null, "Rizwana, Archana"))
        assertEquals("Weekly review", sessionDisplayTitle("schedule", "Mohor Boys", " Weekly review "))
        assertNull(sessionDisplayTitle("meeting", null, ""))
        assertNull(sessionDisplayTitle(null, null, null))
    }
}
