package com.isf.recorder.ui.components

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Test

class ChatTextTest {
    private val report = """
        **General Overview of the Complete Schedule**
        The session at Mohor Girls involved **storytelling** and *poems*.

        ---

        **Ruksana**

        *1. Learning Activities In Progress*
            *   Scene number two is pending.
            -   Needs more practice.
        # Action tasks
    """.trimIndent()

    @Test fun convertsMarkdownToWhatsAppText() {
        val out = markdownToChatText(report)
        val lines = out.split("\n")
        assertEquals("*General Overview of the Complete Schedule*", lines[0])
        assertEquals("The session at Mohor Girls involved *storytelling* and _poems_.", lines[1])
        assert(out.contains("──────────"))
        assert(out.contains("*Ruksana*"))
        assert(out.contains("*1. Learning Activities In Progress*"))
        assert(out.contains("  • Scene number two is pending."))
        assert(out.contains("  • Needs more practice."))
        assert(out.contains("*Action tasks*"))
        assertFalse("no double stars left", out.contains("**"))
        assertFalse("no raw star bullets left", Regex("""(?m)^\s*\*\s""").containsMatchIn(out))
    }

    @Test fun plainTranscriptUnchanged() {
        val t = "[00:01] SPEAKER_01: नमस्कार\n[00:05] SPEAKER_02: hello"
        assertEquals(t, markdownToChatText(t))
    }

    @Test fun realReportKeepsEveryLine() {
        val md = javaClass.classLoader!!.getResource("real_report_8k.md")!!.readText()
        val out = markdownToChatText(md)
        // every word of the report survives (only *, #, - formatting marks change)
        fun words(s: String) = Regex("""[\p{L}\p{N}]+""").findAll(s).map { it.value }.toList()
        assertEquals(words(md), words(out))
        val lastMd = md.trim().lines().last { it.isNotBlank() }
        assert(out.contains(Regex("""[\p{L}\p{N}]+""").findAll(lastMd).last().value))
    }
}
