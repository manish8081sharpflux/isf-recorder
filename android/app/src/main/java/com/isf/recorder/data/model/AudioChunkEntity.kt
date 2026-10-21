package com.isf.recorder.data.model

import androidx.room.Entity
import androidx.room.Index
import androidx.room.PrimaryKey

@Entity(
    tableName = "audio_chunks",
    indices = [
        Index(value = ["sessionId", "chunkIndex"], unique = true),
        Index(value = ["isUploaded"])
    ]
)
data class AudioChunkEntity(
    @PrimaryKey(autoGenerate = true)
    val id: Long = 0,
    val sessionId: String,
    val chunkIndex: Int,
    val filePath: String,
    val durationSeconds: Float,
    val sizeBytes: Long,
    val checksum: String,
    val isUploaded: Boolean = false,
    val uploadedAt: Long? = null,
    val createdAt: Long = System.currentTimeMillis()
)
