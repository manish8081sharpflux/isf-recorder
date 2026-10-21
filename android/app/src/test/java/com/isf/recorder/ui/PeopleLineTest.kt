package com.isf.recorder.ui

import com.isf.recorder.ui.components.recordingPeopleLine
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class PeopleLineTest {
    private fun rec(title: String, mode: String = "schedule") = mapOf<String, Any?>(
        "title" to title, "mode" to mode, "balgruh_name" to "ShriRamkrushna Varkari Sanstha",
        "assistant_coaches" to listOf(mapOf("name" to "archana"), mapOf("name" to "sanket")),
        "participants" to listOf(mapOf("name" to "Asmita"), mapOf("name" to "Pralay")))

    @Test
    fun untouchedScheduleTitleIsNotRepeated() {
        assertNull(recordingPeopleLine(rec("ShriRamkrushna Varkari Sanstha · archana, sanket")))
    }

    @Test
    fun editedTitleThatNamesEveryCoachIsNotRepeated() {   // the 21 Oct bug
        assertNull(recordingPeopleLine(rec("Shri Ramkrushna VS - Archana, Sanket")))
    }

    @Test
    fun titleWithoutTheCoachesStillShowsTheLine() {
        assertEquals("ShriRamkrushna Varkari Sanstha · archana, sanket", recordingPeopleLine(rec("Morning class")))
        assertEquals("ShriRamkrushna Varkari Sanstha · archana, sanket", recordingPeopleLine(rec("Story time with Archana")))
    }

    @Test
    fun meetings() {
        assertNull(recordingPeopleLine(rec("Planning with Asmita and Pralay", mode = "meeting")))
        assertEquals("Asmita, Pralay", recordingPeopleLine(rec("Planning", mode = "meeting")))
    }
}
