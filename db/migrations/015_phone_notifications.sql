-- 015_phone_notifications.sql
-- Phone notifications (21 Oct 2026): the server saves one row per person per notification; the app collects
-- new rows (every 15 min in the background and on every app open) and shows them as phone notifications.
-- Kinds: no_recording (coach, 6 PM), report_edited (coach), admin_report_ready, admin_daily_summary (8 PM),
-- admin_failed, call_shared (admins), announcement (everyone, sent by a super admin).
-- Per-person on/off switches live in user.notify_prefs ({} = defaults). Additive and idempotent.
-- Run: psql -v ON_ERROR_STOP=1 -f 015_phone_notifications.sql -d "$DATABASE_URL"

BEGIN;

CREATE TABLE IF NOT EXISTS isf_recorder.user_notification (
    id            BIGSERIAL PRIMARY KEY,
    org_id        UUID NOT NULL,
    user_id       UUID NOT NULL REFERENCES isf_recorder."user"(id) ON DELETE CASCADE,
    kind          TEXT NOT NULL,
    title         TEXT NOT NULL,
    body          TEXT NOT NULL DEFAULT '',
    recording_id  UUID REFERENCES isf_recorder.recording(id) ON DELETE CASCADE,
    dedupe_key    TEXT,                      -- the same event never notifies the same person twice
    created_by    UUID REFERENCES isf_recorder."user"(id) ON DELETE SET NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS user_notification_user_idx ON isf_recorder.user_notification (user_id, id);
CREATE UNIQUE INDEX IF NOT EXISTS user_notification_dedupe_idx
    ON isf_recorder.user_notification (user_id, dedupe_key) WHERE dedupe_key IS NOT NULL;

ALTER TABLE isf_recorder."user"
    ADD COLUMN IF NOT EXISTS notify_prefs JSONB NOT NULL DEFAULT '{}'::jsonb;

UPDATE isf_recorder.app_config SET value = '015', updated_at = NOW()
 WHERE key = 'schema_version' AND value < '015';

COMMIT;
