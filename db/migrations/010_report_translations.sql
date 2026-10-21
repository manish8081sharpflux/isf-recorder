-- 010_report_translations.sql
-- Report translations (13 Oct 2026): Hindi / Marathi versions of a report, made on request and saved
-- with a hash of the English text they were made from (a changed report gets a fresh translation).
-- Additive and idempotent.
-- Run: psql -v ON_ERROR_STOP=1 -f 010_report_translations.sql -d "$DATABASE_URL"

BEGIN;

ALTER TABLE isf_recorder.recording_report
    ADD COLUMN IF NOT EXISTS translations JSONB NOT NULL DEFAULT '{}'::jsonb;   -- {"hi": {"text", "source_hash"}, "mr": {...}}

UPDATE isf_recorder.app_config SET value = '010', updated_at = NOW()
 WHERE key = 'schema_version' AND value < '010';

COMMIT;
