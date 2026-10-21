-- ═══════════════════════════════════════════════════════════════════════════
-- ISF Recorder — PostgreSQL Schema
-- Schema: isf_recorder (within groundupfactory database)
-- ═══════════════════════════════════════════════════════════════════════════
-- Run with: psql -d groundupfactory -f schema.sql
-- Safe to run multiple times (all CREATE IF NOT EXISTS).
-- ═══════════════════════════════════════════════════════════════════════════

CREATE SCHEMA IF NOT EXISTS isf_recorder;

-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
-- Table 1: Organizations (multi-business support)
-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CREATE TABLE IF NOT EXISTS isf_recorder.organization (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            TEXT NOT NULL,                      -- "Ground Up", "ISF", etc.
    slug            TEXT UNIQUE NOT NULL,                -- "ground-up", "isf"
    logo_url        TEXT,
    settings        JSONB DEFAULT '{}'::jsonb,           -- org-level config
    is_active       BOOLEAN DEFAULT TRUE,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
-- Table 2: Users (admin / staff roles)
-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CREATE TABLE IF NOT EXISTS isf_recorder.user (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id              UUID NOT NULL REFERENCES isf_recorder.organization(id),
    name                TEXT NOT NULL,
    phone               TEXT NOT NULL,                   -- primary identifier (WhatsApp OTP login)
    role                TEXT NOT NULL DEFAULT 'staff',    -- 'admin' | 'staff'
    pin_hash            TEXT,                             -- optional quick-unlock PIN (bcrypt)
    device_id           TEXT,                             -- current device fingerprint
    preferred_language  TEXT DEFAULT 'hi',                -- 'hi' | 'en' | 'mr' | 'auto'
    is_active           BOOLEAN DEFAULT TRUE,
    settings            JSONB DEFAULT '{}'::jsonb,        -- user preferences
    last_seen_at        TIMESTAMPTZ,
    created_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_isfrec_user_phone
    ON isf_recorder.user (phone);
CREATE INDEX IF NOT EXISTS idx_isfrec_user_org
    ON isf_recorder.user (org_id, is_active);

-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
-- Table 3: Recordings (each session from ON → OFF)
-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CREATE TABLE IF NOT EXISTS isf_recorder.recording (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id             UUID NOT NULL REFERENCES isf_recorder.user(id),
    org_id              UUID NOT NULL REFERENCES isf_recorder.organization(id),
    session_id          TEXT NOT NULL UNIQUE,             -- client-generated UUID per session
    title               TEXT,                             -- AI-generated or user-set
    -- Status lifecycle: recording → uploading → processing → completed → error
    status              TEXT NOT NULL DEFAULT 'recording',
    -- Audio metadata
    mic_source          TEXT DEFAULT 'built_in',          -- built_in | wired | usb | bluetooth
    sample_rate         INTEGER DEFAULT 16000,
    total_chunks        INTEGER DEFAULT 0,
    synced_chunks       INTEGER DEFAULT 0,
    duration_seconds    INTEGER DEFAULT 0,
    audio_file_path     TEXT,                             -- assembled file path on server
    file_size_bytes     BIGINT DEFAULT 0,
    -- Transcription (Gemini 3.5 Transcribe for now)
    transcript          TEXT,
    stt_engine          TEXT DEFAULT 'gemini',            -- gemini (future: sarvam, deepgram)
    stt_confidence      FLOAT,
    detected_language   TEXT,                             -- auto-detected primary language
    -- Intelligence (extracted by Gemini)
    overview            TEXT,
    key_points          JSONB DEFAULT '[]'::jsonb,
    decisions           JSONB DEFAULT '[]'::jsonb,
    action_items        JSONB DEFAULT '[]'::jsonb,
    -- Retention system
    is_starred          BOOLEAN DEFAULT FALSE,            -- ⭐ starred = never auto-delete audio
    starred_by          UUID REFERENCES isf_recorder.user(id),
    starred_at          TIMESTAMPTZ,
    audio_deleted_at    TIMESTAMPTZ,                      -- set when 7-day cleanup removes audio file
    -- Soft delete & metadata
    is_deleted          BOOLEAN DEFAULT FALSE,
    device_info         JSONB DEFAULT '{}'::jsonb,        -- {model, os_version, app_version}
    -- Pause tracking: [{reason:"mic_conflict"|"user", paused_at:"...", resumed_at:"..."}]
    pauses              JSONB DEFAULT '[]'::jsonb,
    error_message       TEXT,                             -- pipeline error diagnostics if status = 'error'
    -- Timestamps
    started_at          TIMESTAMPTZ NOT NULL,
    finished_at         TIMESTAMPTZ,
    created_at          TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE isf_recorder.recording ADD COLUMN IF NOT EXISTS error_message TEXT;

CREATE INDEX IF NOT EXISTS idx_isfrec_rec_user
    ON isf_recorder.recording (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_isfrec_rec_org
    ON isf_recorder.recording (org_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_isfrec_rec_status
    ON isf_recorder.recording (status);
CREATE INDEX IF NOT EXISTS idx_isfrec_rec_session
    ON isf_recorder.recording (session_id);
CREATE INDEX IF NOT EXISTS idx_isfrec_rec_starred
    ON isf_recorder.recording (is_starred) WHERE is_starred = TRUE;

-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
-- Table 4: Audio Chunks (tracking upload queue from phone → server)
-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CREATE TABLE IF NOT EXISTS isf_recorder.audio_chunk (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    recording_id        UUID NOT NULL REFERENCES isf_recorder.recording(id) ON DELETE CASCADE,
    chunk_index         INTEGER NOT NULL,                 -- sequential: 0, 1, 2, ...
    file_path           TEXT NOT NULL,                    -- server-side storage path
    duration_seconds    FLOAT NOT NULL DEFAULT 0,
    size_bytes          BIGINT NOT NULL DEFAULT 0,
    checksum            TEXT,                              -- SHA256 for dedup / integrity
    -- Status: received → assembled | error
    status              TEXT NOT NULL DEFAULT 'received',
    uploaded_at         TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_isfrec_chunk_rec
    ON isf_recorder.audio_chunk (recording_id, chunk_index);
CREATE UNIQUE INDEX IF NOT EXISTS idx_isfrec_chunk_dedup
    ON isf_recorder.audio_chunk (recording_id, chunk_index);

-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
-- Table 5: Transcript Segments (speaker diarization + timestamps)
-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CREATE TABLE IF NOT EXISTS isf_recorder.transcript_segment (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    recording_id    UUID NOT NULL REFERENCES isf_recorder.recording(id) ON DELETE CASCADE,
    speaker         TEXT DEFAULT 'SPEAKER_00',            -- SPEAKER_00, SPEAKER_01, ...
    speaker_id      INTEGER DEFAULT 0,
    text            TEXT NOT NULL,
    language        TEXT,                                  -- detected language of this segment
    start_time      FLOAT NOT NULL DEFAULT 0.0,           -- seconds from recording start
    end_time        FLOAT NOT NULL DEFAULT 0.0,
    confidence      FLOAT,                                -- STT confidence score
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_isfrec_seg_rec
    ON isf_recorder.transcript_segment (recording_id, start_time);

-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
-- Table 6: Action Items (extracted from recordings by AI)
-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CREATE TABLE IF NOT EXISTS isf_recorder.action_item (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    recording_id    UUID NOT NULL REFERENCES isf_recorder.recording(id) ON DELETE CASCADE,
    org_id          UUID NOT NULL REFERENCES isf_recorder.organization(id),
    user_id         UUID REFERENCES isf_recorder.user(id),  -- who recorded it
    assigned_to     TEXT,                                  -- name or identifier of assignee
    description     TEXT NOT NULL,
    urgency         TEXT DEFAULT 'normal',                  -- low | normal | high | critical
    due_date        DATE,
    completed       BOOLEAN DEFAULT FALSE,
    completed_at    TIMESTAMPTZ,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_isfrec_action_rec
    ON isf_recorder.action_item (recording_id);
CREATE INDEX IF NOT EXISTS idx_isfrec_action_org
    ON isf_recorder.action_item (org_id, completed, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_isfrec_action_user
    ON isf_recorder.action_item (user_id, completed);

-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
-- Table 7: Audit Log (who did what, when)
-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CREATE TABLE IF NOT EXISTS isf_recorder.audit_log (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID REFERENCES isf_recorder.user(id),
    org_id          UUID REFERENCES isf_recorder.organization(id),
    action          TEXT NOT NULL,                          -- login | record_start | upload | transcribe | star | delete | ...
    target_type     TEXT,                                   -- recording | user | org | action_item
    target_id       UUID,
    metadata        JSONB DEFAULT '{}'::jsonb,              -- extra context
    ip_address      TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_isfrec_audit_org
    ON isf_recorder.audit_log (org_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_isfrec_audit_user
    ON isf_recorder.audit_log (user_id, created_at DESC);

-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
-- Table 8: Chat Templates (admin-managed prompt templates for AI chat)
-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CREATE TABLE IF NOT EXISTS isf_recorder.chat_template (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id          UUID NOT NULL REFERENCES isf_recorder.organization(id),
    created_by      UUID NOT NULL REFERENCES isf_recorder.user(id),
    title           TEXT NOT NULL,                       -- "Daily Report", "Hinglish Summary", etc.
    prompt          TEXT NOT NULL,                       -- The instruction for Gemini
    example_output  TEXT,                                -- Example of expected output format
    icon            TEXT DEFAULT '📋',                   -- Emoji icon for the template
    is_active       BOOLEAN DEFAULT TRUE,
    sort_order      INTEGER DEFAULT 0,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_isfrec_template_org
    ON isf_recorder.chat_template (org_id, is_active, sort_order);

-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
-- Table 9: Chat Conversations (per-session or all-sessions AI chats)
-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CREATE TABLE IF NOT EXISTS isf_recorder.chat_conversation (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id             UUID NOT NULL REFERENCES isf_recorder.user(id),
    org_id              UUID NOT NULL REFERENCES isf_recorder.organization(id),
    -- For per-session chat: set to the recording ID; for all-sessions: NULL
    recording_id        UUID REFERENCES isf_recorder.recording(id) ON DELETE CASCADE,
    mode                TEXT NOT NULL DEFAULT 'session',     -- 'session' | 'all_sessions'
    -- Cached context snapshot (loaded once on first message, avoids re-querying)
    context_snapshot    JSONB DEFAULT '{}'::jsonb,
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_isfrec_chat_conv_user
    ON isf_recorder.chat_conversation (user_id, recording_id);
CREATE INDEX IF NOT EXISTS idx_isfrec_chat_conv_mode
    ON isf_recorder.chat_conversation (user_id, mode);

-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
-- Table 10: Chat Messages (7-day retention, metadata row preserved)
-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CREATE TABLE IF NOT EXISTS isf_recorder.chat_message (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id     UUID NOT NULL REFERENCES isf_recorder.chat_conversation(id) ON DELETE CASCADE,
    role                TEXT NOT NULL,                       -- 'user' | 'assistant'
    content             TEXT NOT NULL,
    -- Track which template was used (if any)
    template_id         UUID REFERENCES isf_recorder.chat_template(id),
    created_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_isfrec_chat_msg_conv
    ON isf_recorder.chat_message (conversation_id, created_at ASC);

-- ═══════════════════════════════════════════════════════════════════════════
-- Seed: Default organization (ISF)
-- ═══════════════════════════════════════════════════════════════════════════
INSERT INTO isf_recorder.organization (name, slug, settings)
VALUES ('ISF', 'isf', '{"default_language": "hi", "retention_days": 7}'::jsonb)
ON CONFLICT (slug) DO NOTHING;

-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
-- Table 11: Gemini Model Pricing
-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CREATE TABLE IF NOT EXISTS isf_recorder.gemini_model_pricing (
    id                                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    gemini_model                        TEXT NOT NULL,
    input_price_per_million             NUMERIC(10, 4) NOT NULL DEFAULT 0.3000,
    output_price_per_million            NUMERIC(10, 4) NOT NULL DEFAULT 2.5000,
    audio_price_per_million             NUMERIC(10, 4) NOT NULL DEFAULT 1.0000,
    cached_input_price_per_million      NUMERIC(10, 4) NOT NULL DEFAULT 0.0300,
    effective_from                      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    effective_to                        TIMESTAMPTZ,
    pricing_currency                    TEXT NOT NULL DEFAULT 'USD',
    is_active                           BOOLEAN NOT NULL DEFAULT TRUE,
    created_at                          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_gemini_pricing_model 
    ON isf_recorder.gemini_model_pricing (gemini_model, is_active);

-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
-- Table 12: App Config (Live Forex & System Settings)
-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CREATE TABLE IF NOT EXISTS isf_recorder.app_config (
    key             TEXT PRIMARY KEY,
    value           TEXT NOT NULL,
    description     TEXT,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
-- Table 13: Gemini Usage Log (Detailed Call Tracking per Recording)
-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CREATE TABLE IF NOT EXISTS isf_recorder.gemini_usage_log (
    id                          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    recording_id                UUID REFERENCES isf_recorder.recording(id) ON DELETE CASCADE,
    operation_type              TEXT NOT NULL,                  -- 'TRANSCRIPTION' | 'SUMMARY' | 'CHAT'
    gemini_model                TEXT NOT NULL,
    gemini_request_id           TEXT,
    request_timestamp           TIMESTAMPTZ NOT NULL,
    response_timestamp          TIMESTAMPTZ,
    duration_ms                 INTEGER DEFAULT 0,
    input_tokens                INTEGER DEFAULT 0,
    output_tokens               INTEGER DEFAULT 0,
    total_tokens                INTEGER DEFAULT 0,
    input_audio_tokens          INTEGER DEFAULT 0,
    cached_input_tokens         INTEGER DEFAULT 0,
    thinking_tokens             INTEGER DEFAULT 0,
    audio_duration_seconds      FLOAT DEFAULT 0.0,
    api_status                  TEXT NOT NULL DEFAULT 'SUCCESS',
    error_message               TEXT,
    pricing_model_id            UUID REFERENCES isf_recorder.gemini_model_pricing(id),
    exchange_rate               NUMERIC(10, 4) NOT NULL DEFAULT 96.00,
    input_cost_usd              NUMERIC(12, 6) DEFAULT 0.0,
    output_cost_usd             NUMERIC(12, 6) DEFAULT 0.0,
    audio_cost_usd              NUMERIC(12, 6) DEFAULT 0.0,
    cached_cost_usd             NUMERIC(12, 6) DEFAULT 0.0,
    total_cost_usd              NUMERIC(12, 6) DEFAULT 0.0,
    total_cost_inr              NUMERIC(12, 4) DEFAULT 0.0,
    currency                    TEXT NOT NULL DEFAULT 'INR',
    raw_usage_json              JSONB DEFAULT '{}'::jsonb,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_gemini_usage_rec 
    ON isf_recorder.gemini_usage_log (recording_id, created_at ASC);
CREATE INDEX IF NOT EXISTS idx_gemini_usage_monthly 
    ON isf_recorder.gemini_usage_log (request_timestamp, api_status);
CREATE INDEX IF NOT EXISTS idx_gemini_usage_req_id 
    ON isf_recorder.gemini_usage_log (gemini_request_id);

