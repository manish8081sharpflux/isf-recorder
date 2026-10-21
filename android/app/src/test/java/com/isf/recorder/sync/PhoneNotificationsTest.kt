package com.isf.recorder.sync

import com.isf.recorder.service.RecorderNotifications
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class PhoneNotificationsTest {
    private val hour = 60 * 60 * 1000L
    private val now = 100 * hour

    @Test
    fun stuckOnlyAfterOneHour() {
        assertFalse(PhoneNotifications.shouldWarnStuck(now - hour / 2, now, 0, on = true))
        assertTrue(PhoneNotifications.shouldWarnStuck(now - hour, now, 0, on = true))
    }

    @Test
    fun noPendingOrSwitchedOffNeverWarns() {
        assertFalse(PhoneNotifications.shouldWarnStuck(null, now, 0, on = true))
        assertFalse(PhoneNotifications.shouldWarnStuck(now - 5 * hour, now, 0, on = false))
    }

    @Test
    fun notRepeatedWithinSixHours() {
        assertFalse(PhoneNotifications.shouldWarnStuck(now - 5 * hour, now, now - 2 * hour, on = true))
        assertTrue(PhoneNotifications.shouldWarnStuck(now - 9 * hour, now, now - 6 * hour, on = true))
    }

    @Test
    fun channels() {
        assertEquals(RecorderNotifications.CHANNEL_MESSAGES, PhoneNotifications.channelFor("announcement"))
        assertEquals(RecorderNotifications.CHANNEL_ALERTS, PhoneNotifications.channelFor("admin_failed"))
        assertEquals(RecorderNotifications.CHANNEL_REPORT, PhoneNotifications.channelFor("report_edited"))
    }
}
