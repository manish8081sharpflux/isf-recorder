-- 011_calls.sql
-- Calls in Jomi (14 Oct 2026): phone-call recordings synced from the phone's own call recorder (replaces the
-- GU Bandhu app). A call is a recording with source = 'call', private to its owner (an admin sees it only when
-- the owner shares it). Tasks found in a call become a to-do list the owner can edit; a daily reminder per user.
-- Additive and idempotent.
-- Run: psql -v ON_ERROR_STOP=1 -f 011_calls.sql -d "$DATABASE_URL"

BEGIN;

ALTER TABLE isf_recorder.recording
    ADD COLUMN IF NOT EXISTS source            TEXT NOT NULL DEFAULT 'recording',   -- recording | call
    ADD COLUMN IF NOT EXISTS contact_name      TEXT,
    ADD COLUMN IF NOT EXISTS contact_phone     TEXT,
    ADD COLUMN IF NOT EXISTS call_type         TEXT,                                -- incoming | outgoing | unknown
    ADD COLUMN IF NOT EXISTS call_app          TEXT,                                -- phone recorder name, e.g. samsung
    ADD COLUMN IF NOT EXISTS shared_with_admin BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS external_id       TEXT;                                -- dedupe key (phone file / old Bandhu row)

ALTER TABLE isf_recorder.recording DROP CONSTRAINT IF EXISTS recording_source_check;
ALTER TABLE isf_recorder.recording
    ADD CONSTRAINT recording_source_check CHECK (source IN ('recording', 'call'));

CREATE UNIQUE INDEX IF NOT EXISTS recording_external_id_key
    ON isf_recorder.recording (external_id) WHERE external_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS recording_source_user_idx
    ON isf_recorder.recording (source, user_id, started_at DESC);

CREATE TABLE IF NOT EXISTS isf_recorder.call_task (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    recording_id UUID REFERENCES isf_recorder.recording(id) ON DELETE CASCADE,   -- NULL = added by hand, not from a call
    user_id      UUID NOT NULL REFERENCES isf_recorder."user"(id) ON DELETE CASCADE,
    text         TEXT NOT NULL,
    assigned_to  TEXT,
    due_text     TEXT,                                                          -- as said in the call: "by Friday"
    done         BOOLEAN NOT NULL DEFAULT FALSE,
    done_at      TIMESTAMPTZ,
    origin       TEXT NOT NULL DEFAULT 'user' CHECK (origin IN ('ai', 'user')),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at   TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS call_task_user_idx ON isf_recorder.call_task (user_id, done) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS call_task_recording_idx ON isf_recorder.call_task (recording_id);

ALTER TABLE isf_recorder."user"
    ADD COLUMN IF NOT EXISTS task_reminder_on   BOOLEAN NOT NULL DEFAULT TRUE,
    ADD COLUMN IF NOT EXISTS task_reminder_time TEXT NOT NULL DEFAULT '09:00';   -- HH:MM, phone local time

UPDATE isf_recorder.app_config SET value = '011', updated_at = NOW()
 WHERE key = 'schema_version' AND value < '011';

COMMIT;
