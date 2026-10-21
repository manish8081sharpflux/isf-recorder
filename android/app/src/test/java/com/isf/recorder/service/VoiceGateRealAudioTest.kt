package com.isf.recorder.service

import org.junit.Assume.assumeTrue
import org.junit.Test
import java.io.File
import java.io.RandomAccessFile
import java.nio.ByteBuffer
import java.nio.ByteOrder

/**
 * Replays the user's real phone recording (Jomi TEST, 4c060f43, chunks 0-6) through [VoiceGate] and
 * prints the kept % per 30 s. Skipped when the files are not on this machine.
 * Dir override: -Djomi.realAudioDir=...
 */
class VoiceGateRealAudioTest {

    private val dir = File(System.getProperty("jomi.realAudioDir")
        ?: "D:/Ground Up/_test_apk/audio/isf/2026-10-12/rec_92148d5d-e747-438f-b9b7-54302aed771d/chunks")

    private fun pcm(f: File): ShortArray {
        RandomAccessFile(f, "r").use { raf ->
            val bytes = ByteArray((raf.length() - 44).toInt())
            raf.seek(44)
            raf.readFully(bytes)
            val sb = ByteBuffer.wrap(bytes).order(ByteOrder.LITTLE_ENDIAN).asShortBuffer()
            return ShortArray(sb.remaining()).also { sb.get(it) }
        }
    }

    @Test
    fun replayRealChunks() {
        val files = (0..6).map { File(dir, "chunk_%04d.wav".format(it)) }
        assumeTrue("real audio not present", files.all { it.exists() })
        val gate = VoiceGate<Int>()
        val n = VoiceGate.FRAME_SAMPLES
        val perPiece = VoiceGate.framesFor(30_000)
        val levels = mutableListOf<Float>()
        val written = HashSet<Int>()
        var idx = 0
        for (f in files) {
            val s = pcm(f)
            var off = 0
            while (off + n <= s.size) {
                val db = VoiceGate.rmsDbfs(s.copyOfRange(off, off + n))
                levels += db
                written += gate.onFrame(idx, db).write
                idx++
                off += n
            }
        }
        println("VoiceGate replay of 4c060f43 chunks 0-6: ${idx} frames (${"%.1f".format(idx * VoiceGate.FRAME_MS / 1000)} s), " +
            "kept ${"%.1f".format(100.0 * written.size / idx)} %")
        var p = 0
        while (p * perPiece < idx) {
            val r = p * perPiece until minOf(idx, (p + 1) * perPiece)
            val kept = r.count { it in written }
            val med = r.map { levels[it] }.filter { it.isFinite() }.sorted().let { if (it.isEmpty()) Float.NaN else it[it.size / 2] }
            println("  %3d-%3d s: kept %5.1f %%  (median %.1f dBFS)".format(p * 30, p * 30 + 30, 100.0 * kept / r.count(), med))
            p++
        }
        println("  final floor ${gate.noiseFloorDbfs} dBFS")
    }
}
