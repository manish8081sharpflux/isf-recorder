package com.isf.recorder.service

import android.content.Context
import android.util.Log
import com.isf.recorder.data.db.AppDatabase
import com.isf.recorder.data.model.AudioChunkEntity
import kotlinx.coroutines.runBlocking
import java.io.File
import java.io.FileOutputStream
import java.io.RandomAccessFile
import java.security.MessageDigest
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.atomic.AtomicLong

/**
 * Writes voice frames into 30 s WAV chunk files for one session.
 *
 * Thread-safety (bug F): every method that touches the open file holds [lock]; the capture thread
 * is the only regular writer, pause/stop/mic-switch close the chunk under the same lock.
 * Chunks are opened lazily on the first write, so an empty chunk is never created.
 * Header fix, SHA-256 and the Room insert run on a single "finalizer" thread (bug G), in order;
 * [awaitFinalized] blocks until every closed chunk is in Room (bug B), then [onChunkSaved] uploads it.
 */
class AudioChunkManager(
    context: Context,
    val sessionId: String,
    private val onChunkSaved: (AudioChunkEntity) -> Unit
) {
    companion object {
        private const val TAG = "AudioChunkManager"
        const val SAMPLE_RATE = 16000
        const val CHANNELS = 1
        const val BYTES_PER_SAMPLE = 2 // 16-bit
        const val BYTES_PER_SECOND = SAMPLE_RATE * CHANNELS * BYTES_PER_SAMPLE // 32,000 bytes/sec
        const val CHUNK_DURATION_SECONDS = 30
        const val TARGET_CHUNK_BYTES = BYTES_PER_SECOND * CHUNK_DURATION_SECONDS // 960,000 bytes (30s)
        private val CHUNK_NAME = Regex("""chunk_(\d{4,})\.wav""")

        fun sessionsRoot(context: Context): File = File(context.filesDir, "sessions")
        fun sessionDir(context: Context, sessionId: String): File = File(sessionsRoot(context), sessionId)
        fun chunkFileName(index: Int): String = String.format("chunk_%04d.wav", index)
        fun chunkIndexOf(file: File): Int? = CHUNK_NAME.matchEntire(file.name)?.groupValues?.get(1)?.toIntOrNull()

        fun sha256(file: File): String {
            val digest = MessageDigest.getInstance("SHA-256")
            file.inputStream().use { fis ->
                val buf = ByteArray(64 * 1024)
                while (true) {
                    val n = fis.read(buf)
                    if (n <= 0) break
                    digest.update(buf, 0, n)
                }
            }
            return digest.digest().joinToString("") { "%02x".format(it) }
        }
    }

    private val chunkDao = AppDatabase.getInstance(context).chunkDao()
    private val sessionDir = sessionDir(context, sessionId).apply { mkdirs() }
    private val finalizer = Executors.newSingleThreadExecutor { r -> Thread(r, "ISF-ChunkFinalizer").apply { isDaemon = true } }

    private val lock = Any()
    private var currentChunkIndex = 0
    private var currentChunkFile: File? = null
    private var currentChunkStream: FileOutputStream? = null
    private var currentChunkBytesWritten = 0L
    private var acceptingWrites = true
    private var byteBuf = ByteArray(VoiceGate.FRAME_SAMPLES * BYTES_PER_SAMPLE)

    private val savedBytes = AtomicLong(0)
    private val completedChunks = AtomicInteger(0)

    /** PCM bytes written so far (= saved voice audio). */
    val totalSavedBytes: Long get() = savedBytes.get()
    val savedVoiceSeconds: Int get() = (savedBytes.get() / BYTES_PER_SECOND).toInt()
    /** Number of closed, non-empty chunks (= next chunk index). */
    fun getCompletedChunkCount(): Int = completedChunks.get()

    /** Appends frames (16-bit mono PCM). Returns false if writes are suspended (paused). */
    fun writeFrames(frames: List<ShortArray>): Boolean = synchronized(lock) {
        if (!acceptingWrites) return false
        for (f in frames) writeLocked(f, f.size)
        true
    }

    private fun writeLocked(samples: ShortArray, count: Int) {
        if (count <= 0) return
        if (currentChunkStream == null && !openLocked()) return
        val need = count * BYTES_PER_SAMPLE
        if (byteBuf.size < need) byteBuf = ByteArray(need)
        for (i in 0 until count) {
            val s = samples[i].toInt()
            byteBuf[2 * i] = (s and 0xff).toByte()
            byteBuf[2 * i + 1] = ((s shr 8) and 0xff).toByte()
        }
        try {
            currentChunkStream!!.write(byteBuf, 0, need)
            currentChunkBytesWritten += need
            savedBytes.addAndGet(need.toLong())
        } catch (e: Exception) {
            Log.e(TAG, "Write failed on chunk $currentChunkIndex: ${e.message}", e)
            closeLocked()
            return
        }
        if (currentChunkBytesWritten >= TARGET_CHUNK_BYTES) closeLocked()
    }

    private fun openLocked(): Boolean = try {
        val file = File(sessionDir, chunkFileName(currentChunkIndex))
        val fos = FileOutputStream(file)
        WavHeaderHelper.writeWavHeader(
            out = fos, totalAudioLen = 0, totalDataLen = 36,
            sampleRate = SAMPLE_RATE.toLong(), channels = CHANNELS, byteRate = BYTES_PER_SECOND.toLong()
        )
        currentChunkFile = file
        currentChunkStream = fos
        currentChunkBytesWritten = 0L
        true
    } catch (e: Exception) {
        Log.e(TAG, "Could not open chunk $currentChunkIndex: ${e.message}", e)
        false
    }

    /** Closes the open chunk (if it holds audio) and queues it for finalization + upload. */
    fun closeCurrentChunk() = synchronized(lock) { closeLocked() }

    /** Pause: refuse further writes and close the open chunk atomically (no stray frame after a pause). */
    fun suspendWrites() = synchronized(lock) {
        acceptingWrites = false
        closeLocked()
    }

    fun resumeWrites() = synchronized(lock) { acceptingWrites = true }

    private fun closeLocked() {
        val file = currentChunkFile ?: return
        val stream = currentChunkStream
        val bytes = currentChunkBytesWritten
        val index = currentChunkIndex
        currentChunkFile = null
        currentChunkStream = null
        currentChunkBytesWritten = 0L
        try {
            stream?.flush()
            stream?.close()
        } catch (e: Exception) {
            Log.w(TAG, "Error closing chunk stream $index: ${e.message}")
        }
        if (bytes <= 0L) {
            file.delete() // never an empty chunk
            return
        }
        currentChunkIndex++
        completedChunks.set(currentChunkIndex)
        finalizer.execute { finalizeChunk(file, index, bytes) }
    }

    /** Runs on the finalizer thread: header, checksum, Room insert, then hands the chunk to the uploader. */
    private fun finalizeChunk(file: File, index: Int, audioBytes: Long) {
        try {
            RandomAccessFile(file, "rw").use { WavHeaderHelper.updateWavHeader(it) }
            val entity = AudioChunkEntity(
                sessionId = sessionId,
                chunkIndex = index,
                filePath = file.absolutePath,
                durationSeconds = audioBytes / BYTES_PER_SECOND.toFloat(),
                sizeBytes = file.length(),
                checksum = sha256(file),
                isUploaded = false
            )
            val id = runBlocking { chunkDao.insertChunk(entity) }
            Log.i(TAG, "Chunk $index saved: ${file.length()} bytes, ${entity.durationSeconds}s")
            onChunkSaved(entity.copy(id = id))
        } catch (t: Throwable) {
            // File stays on disk; OrphanRecovery.registerOrphanFiles picks it up before /finish.
            Log.e(TAG, "Finalizing chunk $index failed: ${t.message}", t)
        }
    }

    /** Blocks until every chunk closed so far is in Room. Call off the main thread. */
    fun awaitFinalized(timeoutMs: Long = 30_000): Boolean = try {
        finalizer.submit { }.get(timeoutMs, TimeUnit.MILLISECONDS)
        true
    } catch (e: Exception) {
        Log.w(TAG, "awaitFinalized timed out/failed: ${e.message}")
        false
    }

    /** Stops accepting finalization work after the queued chunks are done. */
    fun shutdown() {
        finalizer.shutdown()
    }
}
