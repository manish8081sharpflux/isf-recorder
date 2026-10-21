package com.isf.recorder.ui

import com.isf.recorder.ui.components.avatarColors
import com.isf.recorder.ui.components.splitNameArea
import org.junit.Assert.assertEquals
import org.junit.Test

class PickerSheetTest {
    @Test
    fun areaIsThePartAfterTheLastComma() {
        assertEquals("Vikas Anath Ashram" to "Chikhali", splitNameArea("Vikas Anath Ashram, Chikhali"))
        assertEquals("Bhakti shakti, Varkari sansta" to "Alandi", splitNameArea("Bhakti shakti, Varkari sansta, Alandi"))
    }

    @Test
    fun noCommaMeansNoArea() {
        assertEquals("Mohor Girls" to null, splitNameArea("Mohor Girls"))
        assertEquals("Odd," to null, splitNameArea("Odd,"))
    }

    @Test
    fun sameNameAlwaysGetsTheSameColour() {
        assertEquals(avatarColors("archana"), avatarColors("Archana"))
    }
}
