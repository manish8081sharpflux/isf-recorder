package com.isf.recorder.ui

import com.isf.recorder.ui.components.UpdateCheck
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Assert.assertFalse
import org.junit.Test

class UpdateCheckTest {
    private fun latest(code: Int, min: Int = 1, test: Boolean = false) = mapOf<String, Any?>(
        "version_code" to code.toDouble(), "version_name" to "2.0.$code", "notes" to "New", "apk_url" to "https://x/apk",
        "min_supported_version_code" to min.toDouble(), "notify_test_builds" to test)

    @Test
    fun newerBuildPrompts() {
        val u = UpdateCheck.decide(latest(7), 6, "com.isf.recorder")!!
        assertEquals(7, u.versionCode); assertEquals("https://x/apk", u.apkUrl); assertFalse(u.force)
    }

    @Test
    fun sameOrOlderBuildDoesNot() {
        assertNull(UpdateCheck.decide(latest(6), 6, "com.isf.recorder"))
        assertNull(UpdateCheck.decide(latest(5), 6, "com.isf.recorder"))
    }

    @Test
    fun belowMinSupportedIsForced() {
        assertTrue(UpdateCheck.decide(latest(7, min = 7), 6, "com.isf.recorder")!!.force)
    }

    @Test
    fun testBuildsOnlyWhenServerSaysSo() {
        assertNull(UpdateCheck.decide(latest(7), 6, "com.isf.recorder.test"))
        assertEquals(7, UpdateCheck.decide(latest(7, test = true), 6, "com.isf.recorder.test")!!.versionCode)
    }

    @Test
    fun badPayloadIgnored() {
        assertNull(UpdateCheck.decide(mapOf("version_code" to 9.0), 6, "com.isf.recorder"))
        assertNull(UpdateCheck.decide(emptyMap(), 6, "com.isf.recorder"))
    }

    @Test
    fun notificationOncePerVersion() {
        val u = UpdateCheck.decide(latest(7), 6, "com.isf.recorder")
        assertTrue(UpdateCheck.shouldNotify(u, 0))
        assertTrue(UpdateCheck.shouldNotify(u, 6))
        assertFalse(UpdateCheck.shouldNotify(u, 7))      // already shown for 7
        assertFalse(UpdateCheck.shouldNotify(null, 0))   // up to date
    }
}
