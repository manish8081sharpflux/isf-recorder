package com.isf.recorder.ui

import com.isf.recorder.ui.components.fractionAt
import com.isf.recorder.ui.components.thumbGeometry
import org.junit.Assert.assertEquals
import org.junit.Test

class ScrollbarTest {
    @Test
    fun longTextGetsSmallThumbAtThePlaceRead() {
        // a quarter visible, half way through, 1000 px track
        val (top, height) = thumbGeometry(0.5f, 0.25f, 1000f, 36f)
        assertEquals(250f, height, 0.01f)
        assertEquals(375f, top, 0.01f)           // middle of the free space (1000 - 250)
    }

    @Test
    fun veryLongTextKeepsAFingerSizedThumb() {
        val (top, height) = thumbGeometry(1f, 0.001f, 1000f, 36f)
        assertEquals(36f, height, 0.01f)
        assertEquals(964f, top, 0.01f)           // at the end: thumb touches the bottom
    }

    @Test
    fun dragPositionMapsToTheText() {
        assertEquals(0f, fractionAt(0f, 100f, 1000f), 0.001f)        // top
        assertEquals(1f, fractionAt(1000f, 100f, 1000f), 0.001f)     // bottom
        assertEquals(0.5f, fractionAt(500f, 100f, 1000f), 0.001f)    // finger in the middle
        assertEquals(0f, fractionAt(500f, 1000f, 1000f), 0.001f)     // everything fits: no jump
    }
}
