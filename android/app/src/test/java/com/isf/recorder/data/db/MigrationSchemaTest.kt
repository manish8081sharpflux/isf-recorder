package com.isf.recorder.data.db

import com.google.gson.JsonParser
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

/**
 * Room validates the migrated table against the entity at first open; a mismatch would crash the
 * app on update. The exported schema (app/schemas, written by KSP) is Room's own createSql, so the
 * hand-written MIGRATION_1_2 SQL must match it exactly.
 */
class MigrationSchemaTest {
    private val schema = File("schemas/com.isf.recorder.data.db.AppDatabase/2.json")
    private val schemaV3 = File("schemas/com.isf.recorder.data.db.AppDatabase/3.json")
    private val schemaV4 = File("schemas/com.isf.recorder.data.db.AppDatabase/4.json")

    private fun entity(file: File, table: String) =
        JsonParser.parseString(file.readText()).asJsonObject.getAsJsonObject("database")
            .getAsJsonArray("entities").map { it.asJsonObject }.first { it.get("tableName").asString == table }

    @Test
    fun migration2To3AddsExactlyTheNullableCoachesColumn() {
        assertEquals(2, AppDatabase.MIGRATION_2_3.startVersion)
        assertEquals(3, AppDatabase.MIGRATION_2_3.endVersion)
        fun fields(f: File) = entity(f, "recording_sessions").getAsJsonArray("fields").map { it.asJsonObject }
        val v2 = fields(schema).map { it.toString() }
        val v3 = fields(schemaV3)
        val added = v3.filter { it.toString() !in v2 }
        assertEquals(1, added.size)
        assertEquals("assistantCoachesJson", added[0].get("columnName").asString)
        assertEquals("TEXT", added[0].get("affinity").asString)
        assertEquals(false, added[0].get("notNull").asBoolean)
        assertTrue(!added[0].has("defaultValue"))
        assertEquals(v2.size + 1, v3.size)
        assertEquals("ALTER TABLE `recording_sessions` ADD COLUMN `assistantCoachesJson` TEXT", AppDatabase.ADD_ASSISTANT_COACHES_V3)
        // audio_chunks unchanged across versions
        assertEquals(entity(schema, "audio_chunks").toString(), entity(schemaV3, "audio_chunks").toString())
    }

    @Test
    fun migration3To4AddsExactlyTheNullableSilenceGapsColumn() {
        assertTrue("run a build first so KSP exports schema 4", schemaV4.exists())
        assertEquals(3, AppDatabase.MIGRATION_3_4.startVersion)
        assertEquals(4, AppDatabase.MIGRATION_3_4.endVersion)
        fun fields(f: File) = entity(f, "recording_sessions").getAsJsonArray("fields").map { it.asJsonObject }
        val v3 = fields(schemaV3).map { it.toString() }
        val added = fields(schemaV4).filter { it.toString() !in v3 }
        assertEquals(1, added.size)
        assertEquals("silenceGapsJson", added[0].get("columnName").asString)
        assertEquals("TEXT", added[0].get("affinity").asString)
        assertEquals(false, added[0].get("notNull").asBoolean)
        assertTrue(!added[0].has("defaultValue"))
        assertEquals(v3.size + 1, fields(schemaV4).size)
        assertEquals("ALTER TABLE `recording_sessions` ADD COLUMN `silenceGapsJson` TEXT", AppDatabase.ADD_SILENCE_GAPS_V4)
        assertEquals(entity(schemaV3, "audio_chunks").toString(), entity(schemaV4, "audio_chunks").toString())
    }

    private fun createSql(table: String): String {
        val root = JsonParser.parseString(schema.readText()).asJsonObject
        val entities = root.getAsJsonObject("database").getAsJsonArray("entities")
        val e = entities.map { it.asJsonObject }.first { it.get("tableName").asString == table }
        return e.get("createSql").asString.replace("\${TABLE_NAME}", table)
    }

    @Test
    fun migrationCreatesExactlyRoomsRecordingSessionsTable() {
        assertTrue("run a build first so KSP exports the schema", schema.exists())
        assertEquals(createSql("recording_sessions"), AppDatabase.CREATE_RECORDING_SESSIONS_V2)
    }

    @Test
    fun audioChunksTableIsUnchangedFromV1() {
        // v1.2.0 entity (exportSchema was false), unchanged in v2: pending chunks survive the update.
        val v1 = "CREATE TABLE IF NOT EXISTS `audio_chunks` (`id` INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL, " +
            "`sessionId` TEXT NOT NULL, `chunkIndex` INTEGER NOT NULL, `filePath` TEXT NOT NULL, " +
            "`durationSeconds` REAL NOT NULL, `sizeBytes` INTEGER NOT NULL, `checksum` TEXT NOT NULL, " +
            "`isUploaded` INTEGER NOT NULL, `uploadedAt` INTEGER, `createdAt` INTEGER NOT NULL)"
        assertEquals(v1, createSql("audio_chunks"))
    }

    @Test
    fun migrationIsVersion1To2() {
        assertEquals(1, AppDatabase.MIGRATION_1_2.startVersion)
        assertEquals(2, AppDatabase.MIGRATION_1_2.endVersion)
    }
}
