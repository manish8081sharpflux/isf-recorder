-- 017_name_memory.sql
-- Name memory (21 Oct 2026): the transcription AI misheard names ("Bhavnesh" → "Pavnesh"). Jomi now learns names
-- by itself, with no lists to type:
--   - heard:     after each report, the names in it are counted per Balgruha (often heard = sent to the AI next time)
--   - corrected: when someone edits a report and changes a name, the wrong spelling is remembered and fixed
--                automatically in later transcripts and reports
-- balgruh_name '' = everywhere (meetings, calls). Admins can remove a wrong entry (is_deleted). Additive, idempotent.
-- Run: psql -v ON_ERROR_STOP=1 -f 017_name_memory.sql -d "$DATABASE_URL"

BEGIN;

CREATE TABLE IF NOT EXISTS isf_recorder.name_memory (
    id               BIGSERIAL PRIMARY KEY,
    org_id           UUID NOT NULL,
    balgruh_name     TEXT NOT NULL DEFAULT '',
    name             TEXT NOT NULL,                       -- the right spelling
    wrong_spellings  JSONB NOT NULL DEFAULT '[]'::jsonb,  -- spellings people corrected to [name]
    heard_count      INTEGER NOT NULL DEFAULT 0,
    corrected_count  INTEGER NOT NULL DEFAULT 0,
    is_deleted       BOOLEAN NOT NULL DEFAULT FALSE,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS name_memory_unique_idx
    ON isf_recorder.name_memory (org_id, balgruh_name, lower(name));

UPDATE isf_recorder.app_config SET value = '017', updated_at = NOW()
 WHERE key = 'schema_version' AND value < '017';

COMMIT;
