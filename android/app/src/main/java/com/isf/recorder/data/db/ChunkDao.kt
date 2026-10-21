package com.isf.recorder.data.db

import androidx.room.*
import com.isf.recorder.data.model.AudioChunkEntity
import kotlinx.coroutines.flow.Flow

@Dao
interface ChunkDao {
    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun insertChunk(chunk: AudioChunkEntity): Long

    @Query("SELECT * FROM audio_chunks WHERE isUploaded = 0 ORDER BY createdAt ASC, chunkIndex ASC")
    suspend fun getPendingChunks(): List<AudioChunkEntity>

    @Query("SELECT * FROM audio_chunks WHERE sessionId = :sessionId AND isUploaded = 0 ORDER BY chunkIndex ASC")
    suspend fun getPendingChunksForSession(sessionId: String): List<AudioChunkEntity>

    @Query("SELECT * FROM audio_chunks WHERE sessionId = :sessionId ORDER BY chunkIndex ASC")
    suspend fun getAllChunksForSession(sessionId: String): List<AudioChunkEntity>

    @Query("SELECT * FROM audio_chunks WHERE sessionId = :sessionId ORDER BY chunkIndex ASC")
    fun getChunksForSessionFlow(sessionId: String): Flow<List<AudioChunkEntity>>

    @Query("UPDATE audio_chunks SET isUploaded = 1, uploadedAt = :uploadedAt WHERE id = :chunkId")
    suspend fun markUploaded(chunkId: Long, uploadedAt: Long = System.currentTimeMillis())

    @Query("SELECT COUNT(*) FROM audio_chunks WHERE isUploaded = 0")
    fun getPendingCountFlow(): Flow<Int>

    @Query("SELECT * FROM audio_chunks WHERE sessionId = :sessionId AND chunkIndex = :chunkIndex LIMIT 1")
    suspend fun getChunk(sessionId: String, chunkIndex: Int): AudioChunkEntity?

    @Query("SELECT MAX(chunkIndex) FROM audio_chunks WHERE sessionId = :sessionId")
    suspend fun getMaxChunkIndex(sessionId: String): Int?

    @Query("SELECT DISTINCT sessionId FROM audio_chunks WHERE isUploaded = 0")
    suspend fun getSessionIdsWithPendingChunks(): List<String>

    @Query("DELETE FROM audio_chunks WHERE sessionId = :sessionId")
    suspend fun deleteChunksForSession(sessionId: String)

    @Query("DELETE FROM audio_chunks WHERE isUploaded = 1")
    suspend fun deleteUploadedChunks()
}
