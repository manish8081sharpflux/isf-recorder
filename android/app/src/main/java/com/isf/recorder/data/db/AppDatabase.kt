package com.isf.recorder.data.db

import android.content.Context
import androidx.room.Database
import androidx.room.Room
import androidx.room.RoomDatabase
import androidx.room.migration.Migration
import androidx.sqlite.db.SupportSQLiteDatabase
import com.isf.recorder.data.model.AudioChunkEntity
import com.isf.recorder.data.model.RecordingSessionEntity

@Database(
    entities = [AudioChunkEntity::class, RecordingSessionEntity::class],
    version = 4,
    exportSchema = true
)
abstract class AppDatabase : RoomDatabase() {
    abstract fun chunkDao(): ChunkDao
    abstract fun sessionDao(): SessionDao

    companion object {
        @Volatile
        private var INSTANCE: AppDatabase? = null

        /** Must equal Room's generated createSql for recording_sessions (checked by MigrationSchemaTest). */
        const val CREATE_RECORDING_SESSIONS_V2 =
            "CREATE TABLE IF NOT EXISTS `recording_sessions` (`sessionId` TEXT NOT NULL, `state` TEXT NOT NULL, " +
                "`startedAtMs` INTEGER NOT NULL, `recordingId` TEXT, `micSource` TEXT NOT NULL, `mode` TEXT, " +
                "`balgruhName` TEXT, `assistantCoach` TEXT, `participantsJson` TEXT, `title` TEXT, " +
                "`startSynced` INTEGER NOT NULL, `startAttempts` INTEGER NOT NULL, `totalChunks` INTEGER NOT NULL, " +
                "`voiceSeconds` INTEGER NOT NULL, `sessionSeconds` INTEGER NOT NULL, " +
                "`silenceSkippedSeconds` INTEGER NOT NULL, `stoppedAtMs` INTEGER, `finishedAtMs` INTEGER, " +
                "`updatedAtMs` INTEGER NOT NULL, PRIMARY KEY(`sessionId`))"

        /** v1 (v1.2.0 app) → v2: adds recording_sessions. audio_chunks (pending uploads) is untouched. */
        val MIGRATION_1_2 = object : Migration(1, 2) {
            override fun migrate(db: SupportSQLiteDatabase) {
                db.execSQL(CREATE_RECORDING_SESSIONS_V2)
            }
        }

        /** v2 → v3: multiple assistant coaches. Nullable column, no default (matches Room's schema). */
        const val ADD_ASSISTANT_COACHES_V3 = "ALTER TABLE `recording_sessions` ADD COLUMN `assistantCoachesJson` TEXT"

        val MIGRATION_2_3 = object : Migration(2, 3) {
            override fun migrate(db: SupportSQLiteDatabase) {
                db.execSQL(ADD_ASSISTANT_COACHES_V3)
            }
        }

        /** v3 → v4: skipped-silence markers. Nullable column, no default (matches Room's schema). */
        const val ADD_SILENCE_GAPS_V4 = "ALTER TABLE `recording_sessions` ADD COLUMN `silenceGapsJson` TEXT"

        val MIGRATION_3_4 = object : Migration(3, 4) {
            override fun migrate(db: SupportSQLiteDatabase) {
                db.execSQL(ADD_SILENCE_GAPS_V4)
            }
        }

        fun getInstance(context: Context): AppDatabase {
            return INSTANCE ?: synchronized(this) {
                INSTANCE ?: Room.databaseBuilder(
                    context.applicationContext,
                    AppDatabase::class.java,
                    "isf_recorder.db"
                )
                    .addMigrations(MIGRATION_1_2, MIGRATION_2_3, MIGRATION_3_4)   // never destructive: pending audio chunks must survive
                    .build()
                    .also { INSTANCE = it }
            }
        }
    }
}
