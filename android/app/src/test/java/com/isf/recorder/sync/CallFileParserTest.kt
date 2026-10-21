package com.isf.recorder.sync

import org.junit.Assert.*
import org.junit.Test
import java.time.Instant
import java.time.ZoneId

class CallFileParserTest {
    private fun ist(ms: Long?) = ms?.let { Instant.ofEpochMilli(it).atZone(ZoneId.of("Asia/Kolkata")).toLocalDateTime().toString() }

    @Test
    fun samsungShortDate() {
        val p = CallFileParser.parse("Call recording Bhavnesh Desai Sir_261011_135446.m4a")
        assertEquals("Bhavnesh Desai Sir", p.contactName)
        assertNull(p.contactPhone)
        assertEquals("2026-10-11T13:54:46", ist(p.callTimeMs))
    }

    @Test
    fun samsungNumberOnlyAndLongDate() {
        val p = CallFileParser.parse("Call recording +919595145587_261011_101500.m4a")
        assertNull(p.contactName)
        assertEquals("+919595145587", p.contactPhone)
        val q = CallFileParser.parse("Call recording Rizwana 2026-10-11 13.54.46.m4a")
        assertEquals("Rizwana", q.contactName)
        assertEquals("2026-10-11T13:54:46", ist(q.callTimeMs))
    }

    @Test
    fun xiaomiAndGenericPatterns() {
        val x = CallFileParser.parse("Rizwana(+919876543210)_20261011135446.mp3")
        assertEquals("Rizwana", x.contactName); assertEquals("+919876543210", x.contactPhone)
        assertEquals("2026-10-11T13:54:46", ist(x.callTimeMs))
        val g = CallFileParser.parse("20261011135446_Asmita Mam.amr")
        assertEquals("Asmita Mam", g.contactName)
        val u = CallFileParser.parse("weird name 9876543210 something.m4a")
        assertEquals("9876543210", u.contactPhone)
    }

    @Test
    fun onlyCallFoldersAreScanned() {
        assertTrue(CallFileParser.isCallFolder("Recordings/Call/"))
        assertTrue(CallFileParser.isCallFolder("MIUI/sound_recorder/call_rec/"))
        assertTrue(CallFileParser.isCallFolder("/storage/emulated/0/Documents/Cube ACR/x.m4a"))
        assertFalse(CallFileParser.isCallFolder("Recordings/Voice Recorder/"))
        assertFalse(CallFileParser.isCallFolder("Music/Recordings/"))
        assertFalse(CallFileParser.isCallFolder("DCIM/Screen recordings/"))
    }
}
