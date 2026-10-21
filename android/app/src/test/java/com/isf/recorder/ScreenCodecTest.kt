package com.isf.recorder

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

/** The open screens are saved as text so Jomi reopens on the same screen after Android closed it (21 Oct). */
class ScreenCodecTest {
    @Test
    fun everyScreenComesBackTheSame() {
        val screens = listOf(
            Screen.Login, Screen.Home, Screen.AllRecordings, Screen.AskJomi, Screen.Settings,
            Screen.Transcript("1b2c3d4e-0000-0000-0000-000000000001"),
            Screen.Report("1b2c3d4e-0000-0000-0000-000000000002"),
            Screen.Chat("session", "rid", "Mohor Girls · Asmita", "Summarise", "conv-1", newChat = false),
            Screen.Chat("all_sessions", newChat = true)
        )
        screens.forEach { assertEquals(it, Screen.decode(it.encode())) }
    }

    @Test
    fun keysAreStableAcrossRestarts() {
        // object screens used to be keyed by toString() ("Screen$Home@1a2b"), which changes every start
        assertEquals("""{"s":"home"}""", Screen.Home.encode())
    }

    @Test
    fun unreadableTextIsIgnored() {
        assertNull(Screen.decode("not json"))
        assertNull(Screen.decode("""{"s":"something-new"}"""))
    }
}
