-- 005_jomi_redesign.sql
-- Jomi redesign (2026-10-07): Meeting/Schedule metadata, Balgruh & coach lists, editable reports,
-- multiple saved chat conversations, user archive / permanent delete, heartbeats, and FK changes so
-- that erasing a recording or deleting a user keeps cost history and recordings.
-- Additive and idempotent: safe to run twice; the v1.2.0 app keeps working.
-- Run: psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f 005_jomi_redesign.sql

BEGIN;

-- ─── recording: session metadata, heartbeat, voice stats ─────────────────────
ALTER TABLE isf_recorder.recording
    ADD COLUMN IF NOT EXISTS mode                      TEXT,
    ADD COLUMN IF NOT EXISTS balgruh_name              TEXT,
    ADD COLUMN IF NOT EXISTS assistant_coach           TEXT,
    ADD COLUMN IF NOT EXISTS participants              JSONB DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS last_heartbeat_at         TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS last_chunk_at             TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS voice_seconds             INTEGER,
    ADD COLUMN IF NOT EXISTS silence_skipped_seconds   INTEGER,
    ADD COLUMN IF NOT EXISTS recorded_by_name          TEXT,
    ADD COLUMN IF NOT EXISTS auto_retranscribe_count   INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS chunks_cleaned_at         TIMESTAMPTZ;

ALTER TABLE isf_recorder.recording DROP CONSTRAINT IF EXISTS recording_mode_check;
ALTER TABLE isf_recorder.recording
    ADD CONSTRAINT recording_mode_check CHECK (mode IS NULL OR mode IN ('schedule', 'meeting'));

-- Recordings outlive their user (permanent user delete keeps recordings with a name snapshot)
ALTER TABLE isf_recorder.recording ALTER COLUMN user_id DROP NOT NULL;

UPDATE isf_recorder.recording r
   SET recorded_by_name = u.name
  FROM isf_recorder."user" u
 WHERE r.user_id = u.id AND r.recorded_by_name IS NULL;

UPDATE isf_recorder.recording r
   SET last_chunk_at = c.last_at
  FROM (SELECT recording_id, MAX(uploaded_at) AS last_at FROM isf_recorder.audio_chunk GROUP BY recording_id) c
 WHERE r.id = c.recording_id AND r.last_chunk_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_isfrec_recording_idle
    ON isf_recorder.recording (status, last_heartbeat_at, last_chunk_at)
    WHERE status = 'recording';
CREATE INDEX IF NOT EXISTS idx_isfrec_recording_mode ON isf_recorder.recording (org_id, mode, started_at DESC);

-- ─── user: archive ───────────────────────────────────────────────────────────
ALTER TABLE isf_recorder."user" ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ;

-- ─── FKs to user: never block a permanent user delete ────────────────────────
ALTER TABLE isf_recorder.recording DROP CONSTRAINT IF EXISTS recording_user_id_fkey;
ALTER TABLE isf_recorder.recording
    ADD CONSTRAINT recording_user_id_fkey FOREIGN KEY (user_id)
    REFERENCES isf_recorder."user"(id) ON DELETE SET NULL;

ALTER TABLE isf_recorder.recording DROP CONSTRAINT IF EXISTS recording_starred_by_fkey;
ALTER TABLE isf_recorder.recording
    ADD CONSTRAINT recording_starred_by_fkey FOREIGN KEY (starred_by)
    REFERENCES isf_recorder."user"(id) ON DELETE SET NULL;

ALTER TABLE isf_recorder.action_item DROP CONSTRAINT IF EXISTS action_item_user_id_fkey;
ALTER TABLE isf_recorder.action_item
    ADD CONSTRAINT action_item_user_id_fkey FOREIGN KEY (user_id)
    REFERENCES isf_recorder."user"(id) ON DELETE SET NULL;

ALTER TABLE isf_recorder.audit_log DROP CONSTRAINT IF EXISTS audit_log_user_id_fkey;
ALTER TABLE isf_recorder.audit_log
    ADD CONSTRAINT audit_log_user_id_fkey FOREIGN KEY (user_id)
    REFERENCES isf_recorder."user"(id) ON DELETE SET NULL;

ALTER TABLE isf_recorder.chat_template ALTER COLUMN created_by DROP NOT NULL;
ALTER TABLE isf_recorder.chat_template DROP CONSTRAINT IF EXISTS chat_template_created_by_fkey;
ALTER TABLE isf_recorder.chat_template
    ADD CONSTRAINT chat_template_created_by_fkey FOREIGN KEY (created_by)
    REFERENCES isf_recorder."user"(id) ON DELETE SET NULL;

-- A user's own chats go with them on permanent delete
ALTER TABLE isf_recorder.chat_conversation DROP CONSTRAINT IF EXISTS chat_conversation_user_id_fkey;
ALTER TABLE isf_recorder.chat_conversation
    ADD CONSTRAINT chat_conversation_user_id_fkey FOREIGN KEY (user_id)
    REFERENCES isf_recorder."user"(id) ON DELETE CASCADE;

-- ─── chat: multiple saved conversations ──────────────────────────────────────
ALTER TABLE isf_recorder.chat_conversation ADD COLUMN IF NOT EXISTS title TEXT;
ALTER TABLE isf_recorder.chat_conversation ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT now();
CREATE INDEX IF NOT EXISTS idx_isfrec_conv_user_mode
    ON isf_recorder.chat_conversation (user_id, mode, recording_id, updated_at DESC);

-- Deleting a template must not fail because an old message used it
ALTER TABLE isf_recorder.chat_message DROP CONSTRAINT IF EXISTS chat_message_template_id_fkey;
ALTER TABLE isf_recorder.chat_message
    ADD CONSTRAINT chat_message_template_id_fkey FOREIGN KEY (template_id)
    REFERENCES isf_recorder.chat_template(id) ON DELETE SET NULL;

-- ─── gemini_usage_log: keep ₹ history when a recording is erased ─────────────
ALTER TABLE isf_recorder.gemini_usage_log ADD COLUMN IF NOT EXISTS org_id UUID;
UPDATE isf_recorder.gemini_usage_log l
   SET org_id = r.org_id
  FROM isf_recorder.recording r
 WHERE l.recording_id = r.id AND l.org_id IS NULL;

ALTER TABLE isf_recorder.gemini_usage_log DROP CONSTRAINT IF EXISTS gemini_usage_log_recording_id_fkey;
ALTER TABLE isf_recorder.gemini_usage_log
    ADD CONSTRAINT gemini_usage_log_recording_id_fkey FOREIGN KEY (recording_id)
    REFERENCES isf_recorder.recording(id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS idx_isfrec_usage_org_created ON isf_recorder.gemini_usage_log (org_id, created_at);

-- ─── transcription_run: silent-chunk stats ───────────────────────────────────
ALTER TABLE isf_recorder.transcription_run
    ADD COLUMN IF NOT EXISTS chunks_total          INTEGER,
    ADD COLUMN IF NOT EXISTS skipped_silent_chunks INTEGER;

-- ─── Admin-managed lists (Balgruh names, extra coach names) ──────────────────
CREATE TABLE IF NOT EXISTS isf_recorder.recorder_list_item (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id      UUID NOT NULL REFERENCES isf_recorder.organization(id),
    list_type   TEXT NOT NULL CHECK (list_type IN ('balgruh', 'coach_name')),
    name        TEXT NOT NULL,
    is_active   BOOLEAN NOT NULL DEFAULT TRUE,
    sort_order  INTEGER NOT NULL DEFAULT 0,
    created_by  UUID REFERENCES isf_recorder."user"(id) ON DELETE SET NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_isfrec_list_item_name
    ON isf_recorder.recorder_list_item (org_id, list_type, lower(name));

INSERT INTO isf_recorder.recorder_list_item (org_id, list_type, name, sort_order)
SELECT o.id, 'balgruh', v.name, v.ord
  FROM isf_recorder.organization o
 CROSS JOIN (VALUES ('Mohor Boys', 1), ('Mohor Girls', 2), ('Nirmal Girls', 3)) AS v(name, ord)
 WHERE o.slug = 'isf'
ON CONFLICT DO NOTHING;

-- ─── Editable reports (one per recording) ────────────────────────────────────
CREATE TABLE IF NOT EXISTS isf_recorder.recording_report (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    recording_id   UUID NOT NULL UNIQUE REFERENCES isf_recorder.recording(id) ON DELETE CASCADE,
    template_id    UUID REFERENCES isf_recorder.chat_template(id) ON DELETE SET NULL,
    template_title TEXT,
    content        TEXT NOT NULL DEFAULT '',
    status         TEXT NOT NULL DEFAULT 'generating'
                   CHECK (status IN ('generating', 'draft', 'final', 'error')),
    error_message  TEXT,
    run_id         UUID REFERENCES isf_recorder.transcription_run(id) ON DELETE SET NULL,
    generated_at   TIMESTAMPTZ,
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by     UUID REFERENCES isf_recorder."user"(id) ON DELETE SET NULL,
    finalized_at   TIMESTAMPTZ,
    finalized_by   UUID REFERENCES isf_recorder."user"(id) ON DELETE SET NULL
);

-- ─── Finish bookkeeping (async, idempotent /finish) ──────────────────────────
ALTER TABLE isf_recorder.recording
    ADD COLUMN IF NOT EXISTS finish_requested_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS finish_source       TEXT,      -- client | sync_worker | sweeper | legacy
    ADD COLUMN IF NOT EXISTS expected_chunks     INTEGER,
    ADD COLUMN IF NOT EXISTS pipeline_stage      TEXT;      -- awaiting_chunks|stitching|queued|transcribing|reporting|done|failed
-- Existing finished recordings: a repeated /finish must be a no-op
UPDATE isf_recorder.recording
   SET finish_requested_at = COALESCE(finished_at, created_at), finish_source = 'legacy'
 WHERE status <> 'recording' AND finish_requested_at IS NULL;

-- ─── Erased sessions: late chunks / finish must not re-create them ───────────
CREATE TABLE IF NOT EXISTS isf_recorder.deleted_session (
    session_id     TEXT PRIMARY KEY,
    recording_id   UUID,
    org_id         UUID,
    deleted_by     UUID,
    deleted_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    files_pending  JSONB NOT NULL DEFAULT '[]'::jsonb   -- paths Windows had locked; retried by the sweeper
);

-- ─── One transcription at a time per recording, enforced by the DB ───────────
UPDATE isf_recorder.transcription_run
   SET status = 'error', finished_at = now(), error_message = 'Superseded duplicate run'
 WHERE status = 'processing'
   AND id NOT IN (SELECT DISTINCT ON (recording_id) id
                    FROM isf_recorder.transcription_run
                   WHERE status = 'processing'
                   ORDER BY recording_id, started_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS uq_isfrec_run_one_processing
    ON isf_recorder.transcription_run (recording_id) WHERE status = 'processing';

-- ─── Per-window transcription results (3-min windows; reused on Retry) ───────
CREATE TABLE IF NOT EXISTS isf_recorder.transcription_chunk (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id            UUID NOT NULL REFERENCES isf_recorder.transcription_run(id) ON DELETE CASCADE,
    recording_id      UUID NOT NULL REFERENCES isf_recorder.recording(id) ON DELETE CASCADE,
    idx               INTEGER NOT NULL,
    start_sec         DOUBLE PRECISION NOT NULL,
    end_sec           DOUBLE PRECISION NOT NULL,
    speech_sec        DOUBLE PRECISION,
    status            TEXT NOT NULL DEFAULT 'pending',  -- pending|skipped_silent|done|loop_truncated|failed
    attempts          INTEGER NOT NULL DEFAULT 0,
    model             TEXT,
    segments          JSONB,
    finish_reason     TEXT,
    output_tokens     INTEGER,
    audio_fingerprint TEXT,                              -- size:mtime of the m4a the window came from
    error_message     TEXT,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (run_id, idx)
);
CREATE INDEX IF NOT EXISTS idx_isfrec_tchunk_reuse
    ON isf_recorder.transcription_chunk (recording_id, model, status);

-- ─── Per-organisation recorder config (modes, labels, template per mode) ─────
UPDATE isf_recorder.organization
   SET settings = COALESCE(settings, '{}'::jsonb) || jsonb_build_object(
       'recorder', jsonb_build_object(
           'role_label', 'Coach',
           'modes', jsonb_build_array('schedule', 'meeting'),
           'default_mode', 'schedule',
           'report_templates', jsonb_build_object()
       ))
 WHERE slug = 'isf' AND NOT (COALESCE(settings, '{}'::jsonb) ? 'recorder');

-- Idle auto-finalize applies only to sessions started after this migration (never to old orphans)
INSERT INTO isf_recorder.app_config (key, value, description)
VALUES ('idle_finalize_since', to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"+00:00"'),
        'Sweeper idle-finalize only touches recordings started after this time')
ON CONFLICT (key) DO NOTHING;

INSERT INTO isf_recorder.app_config (key, value, description)
VALUES ('schema_version', '005', 'Last applied isf_recorder migration')
ON CONFLICT (key) DO UPDATE SET value = '005', updated_at = now();

COMMIT;
