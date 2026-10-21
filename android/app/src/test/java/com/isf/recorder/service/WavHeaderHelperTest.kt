package com.isf.recorder.service

import org.junit.Assert.assertEquals
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File
import java.io.FileOutputStream

class WavHeaderHelperTest {
    @get:Rule val tmp = TemporaryFolder()

    /** A chunk as left by a killed process: placeholder header (0 bytes) + PCM data. */
    private fun partialChunk(dataBytes: Int): File {
        val f = tmp.newFile()
        FileOutputStream(f).use { out ->
            WavHeaderHelper.writeWavHeader(out, totalAudioLen = 0, totalDataLen = 36)
            out.write(ByteArray(dataBytes) { (it % 7).toByte() })
        }
        return f
    }

    @Test
    fun repairFixesSizesFromFileLength() {
        val f = partialChunk(32_000)
        assertEquals(0L, WavHeaderHelper.readDataSize(f))
        assertEquals(32_000L, WavHeaderHelper.repairFile(f))
        assertEquals(32_000L, WavHeaderHelper.readDataSize(f))
        assertEquals(44L + 32_000L, f.length())
    }

    @Test
    fun repairDropsTrailingOddByte() {
        val f = partialChunk(1001)
        assertEquals(1000L, WavHeaderHelper.repairFile(f))
        assertEquals(1044L, f.length())
        assertEquals(1000L, WavHeaderHelper.readDataSize(f))
    }

    @Test
    fun headerOnlyChunkHasNoData() {
        assertEquals(0L, WavHeaderHelper.repairFile(partialChunk(0)))
    }

    @Test
    fun garbageIsRejected() {
        val short = tmp.newFile().apply { writeBytes(ByteArray(10)) }
        assertEquals(-1L, WavHeaderHelper.repairFile(short))
        val noRiff = tmp.newFile().apply { writeBytes(ByteArray(100)) }
        assertEquals(-1L, WavHeaderHelper.repairFile(noRiff))
        assertEquals(-1L, WavHeaderHelper.repairFile(File(tmp.root, "missing.wav")))
    }

    @Test
    fun chunkIndexParsing() {
        assertEquals(12, AudioChunkManager.chunkIndexOf(File("chunk_0012.wav")))
        assertEquals(null, AudioChunkManager.chunkIndexOf(File("chunk_12.wav.tmp")))
        assertEquals("chunk_0003.wav", AudioChunkManager.chunkFileName(3))
    }
}
