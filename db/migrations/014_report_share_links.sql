-- 014_report_share_links.sql
-- Report share links (21 Oct 2026): WhatsApp cuts long messages, so Share sends a short message with a
-- link to a web page with the full report. One link per recording, made on the first Share and reused;
-- it never expires. Anyone with the link can read the report (the token is long and random).
-- Deleting the recording deletes its link. Calls never get one. Additive and idempotent.
-- Run: psql -v ON_ERROR_STOP=1 -f 014_report_share_links.sql -d "$DATABASE_URL"

BEGIN;

CREATE TABLE IF NOT EXISTS isf_recorder.report_share (
    token         TEXT PRIMARY KEY,
    recording_id  UUID NOT NULL UNIQUE REFERENCES isf_recorder.recording(id) ON DELETE CASCADE,
    created_by    UUID REFERENCES isf_recorder."user"(id) ON DELETE SET NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_opened_at TIMESTAMPTZ,
    open_count    INTEGER NOT NULL DEFAULT 0
);

UPDATE isf_recorder.app_config SET value = '014', updated_at = NOW()
 WHERE key = 'schema_version' AND value < '014';

COMMIT;
