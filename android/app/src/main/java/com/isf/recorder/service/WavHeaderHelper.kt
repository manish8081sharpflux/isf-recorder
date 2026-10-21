package com.isf.recorder.service

import java.io.File
import java.io.FileOutputStream
import java.io.RandomAccessFile

object WavHeaderHelper {
    /**
     * Writes 44-byte WAV RIFF header to the stream.
     */
    fun writeWavHeader(
        out: FileOutputStream,
        totalAudioLen: Long,
        totalDataLen: Long,
        sampleRate: Long = 16000,
        channels: Int = 1,
        byteRate: Long = 32000
    ) {
        val header = ByteArray(44)
        header[0] = 'R'.code.toByte() // RIFF/WAVE header
        header[1] = 'I'.code.toByte()
        header[2] = 'F'.code.toByte()
        header[3] = 'F'.code.toByte()
        header[4] = (totalDataLen and 0xffL).toByte()
        header[5] = (totalDataLen shr 8 and 0xffL).toByte()
        header[6] = (totalDataLen shr 16 and 0xffL).toByte()
        header[7] = (totalDataLen shr 24 and 0xffL).toByte()
        header[8] = 'W'.code.toByte()
        header[9] = 'A'.code.toByte()
        header[10] = 'V'.code.toByte()
        header[11] = 'E'.code.toByte()
        header[12] = 'f'.code.toByte() // 'fmt ' chunk
        header[13] = 'm'.code.toByte()
        header[14] = 't'.code.toByte()
        header[15] = ' '.code.toByte()
        header[16] = 16 // 4 bytes: size of 'fmt ' chunk
        header[17] = 0
        header[18] = 0
        header[19] = 0
        header[20] = 1 // format = 1 (PCM)
        header[21] = 0
        header[22] = channels.toByte()
        header[23] = 0
        header[24] = (sampleRate and 0xffL).toByte()
        header[25] = (sampleRate shr 8 and 0xffL).toByte()
        header[26] = (sampleRate shr 16 and 0xffL).toByte()
        header[27] = (sampleRate shr 24 and 0xffL).toByte()
        header[28] = (byteRate and 0xffL).toByte()
        header[29] = (byteRate shr 8 and 0xffL).toByte()
        header[30] = (byteRate shr 16 and 0xffL).toByte()
        header[31] = (byteRate shr 24 and 0xffL).toByte()
        header[32] = (channels * 2).toByte() // block align (16 bit = 2 bytes)
        header[33] = 0
        header[34] = 16 // bits per sample
        header[35] = 0
        header[36] = 'd'.code.toByte()
        header[37] = 'a'.code.toByte()
        header[38] = 't'.code.toByte()
        header[39] = 'a'.code.toByte()
        header[40] = (totalAudioLen and 0xffL).toByte()
        header[41] = (totalAudioLen shr 8 and 0xffL).toByte()
        header[42] = (totalAudioLen shr 16 and 0xffL).toByte()
        header[43] = (totalAudioLen shr 24 and 0xffL).toByte()
        out.write(header, 0, 44)
    }

    /**
     * Updates header with actual file size after recording stops.
     */
    fun updateWavHeader(raf: RandomAccessFile) {
        val totalAudioLen = raf.length() - 44
        val totalDataLen = totalAudioLen + 36

        // RIFF size
        raf.seek(4)
        raf.write((totalDataLen and 0xffL).toInt())
        raf.write((totalDataLen shr 8 and 0xffL).toInt())
        raf.write((totalDataLen shr 16 and 0xffL).toInt())
        raf.write((totalDataLen shr 24 and 0xffL).toInt())

        // data size
        raf.seek(40)
        raf.write((totalAudioLen and 0xffL).toInt())
        raf.write((totalAudioLen shr 8 and 0xffL).toInt())
        raf.write((totalAudioLen shr 16 and 0xffL).toInt())
        raf.write((totalAudioLen shr 24 and 0xffL).toInt())
    }

    const val HEADER_SIZE = 44

    /**
     * Repairs a chunk left behind by a killed process (header still says 0 bytes): drops a trailing
     * odd byte (16-bit samples) and rewrites the RIFF/data sizes from the real file length.
     * Returns the number of PCM data bytes, or -1 when the file is not a usable WAV (missing,
     * shorter than the header, or no RIFF magic).
     */
    fun repairFile(file: File): Long {
        if (!file.isFile || file.length() < HEADER_SIZE) return -1
        RandomAccessFile(file, "rw").use { raf ->
            val magic = ByteArray(4)
            raf.seek(0)
            raf.readFully(magic)
            if (String(magic, Charsets.US_ASCII) != "RIFF") return -1
            var data = raf.length() - HEADER_SIZE
            if (data % 2L != 0L) {
                raf.setLength(raf.length() - 1)
                data -= 1
            }
            updateWavHeader(raf)
            return data
        }
    }

    /** Reads the little-endian "data" chunk size field (bytes 40..43). */
    fun readDataSize(file: File): Long {
        RandomAccessFile(file, "r").use { raf ->
            raf.seek(40)
            val b = ByteArray(4)
            raf.readFully(b)
            return (b[0].toLong() and 0xff) or ((b[1].toLong() and 0xff) shl 8) or
                ((b[2].toLong() and 0xff) shl 16) or ((b[3].toLong() and 0xff) shl 24)
        }
    }
}
