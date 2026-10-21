-- 016_director_pov.sql
-- Director's POV (21 Oct 2026): one tap in Previous recordings (admins only) makes the Director's POV of a
-- recording from the org's "Director…" AI chat template and saves it, so the next tap opens it instantly.
-- Deleting the recording deletes it. Additive and idempotent.
-- Run: psql -v ON_ERROR_STOP=1 -f 016_director_pov.sql -d "$DATABASE_URL"

BEGIN;

CREATE TABLE IF NOT EXISTS isf_recorder.recording_director_pov (
    recording_id   UUID PRIMARY KEY REFERENCES isf_recorder.recording(id) ON DELETE CASCADE,
    template_id    UUID REFERENCES isf_recorder.chat_template(id) ON DELETE SET NULL,
    template_title TEXT,
    content        TEXT NOT NULL DEFAULT '',
    status         TEXT NOT NULL DEFAULT 'generating' CHECK (status IN ('generating', 'ready', 'error')),
    error_message  TEXT,
    started_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    generated_at   TIMESTAMPTZ,
    generated_by   UUID REFERENCES isf_recorder."user"(id) ON DELETE SET NULL
);

UPDATE isf_recorder.app_config SET value = '016', updated_at = NOW()
 WHERE key = 'schema_version' AND value < '016';

COMMIT;
