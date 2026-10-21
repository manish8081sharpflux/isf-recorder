-- 013_voice_assistant.sql
-- Jomi voice assistant (16 Oct 2026): the round button at the bottom right for admins and super admins.
-- The phone does speech-to-text and text-to-speech; the server keeps every voice chat so admins can open
-- one person's chats from Settings (below AI models). Additive and idempotent.
-- Run: psql -v ON_ERROR_STOP=1 -f 013_voice_assistant.sql -d "$DATABASE_URL"

BEGIN;

CREATE TABLE IF NOT EXISTS isf_recorder.voice_chat (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id      UUID NOT NULL,
    user_id     UUID NOT NULL REFERENCES isf_recorder."user"(id) ON DELETE CASCADE,
    started_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS voice_chat_org_user_idx ON isf_recorder.voice_chat (org_id, user_id, last_at DESC);

CREATE TABLE IF NOT EXISTS isf_recorder.voice_chat_message (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    chat_id     UUID NOT NULL REFERENCES isf_recorder.voice_chat(id) ON DELETE CASCADE,
    role        TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    text        TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS voice_chat_message_chat_idx ON isf_recorder.voice_chat_message (chat_id, created_at);

UPDATE isf_recorder.app_config SET value = '013', updated_at = NOW()
 WHERE key = 'schema_version' AND value < '013';

COMMIT;
