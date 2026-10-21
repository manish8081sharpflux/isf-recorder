-- ═══════════════════════════════════════════════════════════════════════════
-- Migration: Gemini API Usage Tracking & Cost Logging
-- Schema: isf_recorder
-- Safe to re-run (IF NOT EXISTS guards)
-- ═══════════════════════════════════════════════════════════════════════════

CREATE SCHEMA IF NOT EXISTS isf_recorder;

-- 1. Model Pricing Configuration
CREATE TABLE IF NOT EXISTS isf_recorder.gemini_model_pricing (
    id                                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    gemini_model                        TEXT NOT NULL,
    input_price_per_million             NUMERIC(10, 4) NOT NULL DEFAULT 0.3000, -- USD per 1M text tokens
    output_price_per_million            NUMERIC(10, 4) NOT NULL DEFAULT 2.5000, -- USD per 1M output/reasoning tokens
    audio_price_per_million             NUMERIC(10, 4) NOT NULL DEFAULT 1.0000, -- USD per 1M audio tokens
    cached_input_price_per_million      NUMERIC(10, 4) NOT NULL DEFAULT 0.0300, -- USD per 1M cached prompt tokens
    effective_from                      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    effective_to                        TIMESTAMPTZ,
    pricing_currency                    TEXT NOT NULL DEFAULT 'USD',
    is_active                           BOOLEAN NOT NULL DEFAULT TRUE,
    created_at                          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_gemini_pricing_model 
    ON isf_recorder.gemini_model_pricing (gemini_model, is_active);

-- Seed Initial Model Pricing (USD rates per 1M tokens)
INSERT INTO isf_recorder.gemini_model_pricing 
(gemini_model, input_price_per_million, output_price_per_million, audio_price_per_million, cached_input_price_per_million)
SELECT 'gemini-2.5-flash', 0.3000, 2.5000, 1.0000, 0.0300
WHERE NOT EXISTS (SELECT 1 FROM isf_recorder.gemini_model_pricing WHERE gemini_model = 'gemini-2.5-flash');

INSERT INTO isf_recorder.gemini_model_pricing 
(gemini_model, input_price_per_million, output_price_per_million, audio_price_per_million, cached_input_price_per_million)
SELECT 'gemini-3.5-flash', 0.3000, 2.5000, 1.0000, 0.0300
WHERE NOT EXISTS (SELECT 1 FROM isf_recorder.gemini_model_pricing WHERE gemini_model = 'gemini-3.5-flash');

INSERT INTO isf_recorder.gemini_model_pricing 
(gemini_model, input_price_per_million, output_price_per_million, audio_price_per_million, cached_input_price_per_million)
SELECT 'gemini-3.1-flash-lite', 0.2500, 1.5000, 0.5000, 0.0200
WHERE NOT EXISTS (SELECT 1 FROM isf_recorder.gemini_model_pricing WHERE gemini_model = 'gemini-3.1-flash-lite');

-- 2. App Configuration (Live Exchange Rate & Global Settings)
CREATE TABLE IF NOT EXISTS isf_recorder.app_config (
    key             TEXT PRIMARY KEY,
    value           TEXT NOT NULL,
    description     TEXT,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Seed exchange rate (defaults to 96.00, updated dynamically via internet feed)
INSERT INTO isf_recorder.app_config (key, value, description)
VALUES 
('usd_to_inr_exchange_rate', '96.00', 'Live USD to INR exchange rate for Gemini cost calculation'),
('forex_auto_sync', 'true', 'Whether to automatically sync exchange rate from internet feeds daily'),
('last_forex_sync_at', NOW()::text, 'Timestamp of last live exchange rate sync')
ON CONFLICT (key) DO NOTHING;

-- 3. Gemini Usage Log (Granular API Call Records)
CREATE TABLE IF NOT EXISTS isf_recorder.gemini_usage_log (
    id                          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    recording_id                UUID REFERENCES isf_recorder.recording(id) ON DELETE CASCADE,
    operation_type              TEXT NOT NULL,                  -- 'TRANSCRIPTION' | 'SUMMARY' | 'CHAT'
    gemini_model                TEXT NOT NULL,                  -- 'gemini-2.5-flash', etc.
    gemini_request_id           TEXT,                           -- Correlation / idempotency ID
    request_timestamp           TIMESTAMPTZ NOT NULL,
    response_timestamp          TIMESTAMPTZ,
    duration_ms                 INTEGER DEFAULT 0,
    
    -- Token breakdown directly from Gemini usage_metadata
    input_tokens                INTEGER DEFAULT 0,
    output_tokens               INTEGER DEFAULT 0,
    total_tokens                INTEGER DEFAULT 0,
    input_audio_tokens          INTEGER DEFAULT 0,
    cached_input_tokens         INTEGER DEFAULT 0,
    thinking_tokens             INTEGER DEFAULT 0,
    audio_duration_seconds      FLOAT DEFAULT 0.0,
    
    -- Call status & error details
    api_status                  TEXT NOT NULL DEFAULT 'SUCCESS', -- 'SUCCESS' | 'FAILED'
    error_message               TEXT,
    
    -- Cost calculation (in INR)
    pricing_model_id            UUID REFERENCES isf_recorder.gemini_model_pricing(id),
    exchange_rate               NUMERIC(10, 4) NOT NULL DEFAULT 96.00,
    input_cost_usd              NUMERIC(12, 6) DEFAULT 0.0,
    output_cost_usd             NUMERIC(12, 6) DEFAULT 0.0,
    audio_cost_usd              NUMERIC(12, 6) DEFAULT 0.0,
    cached_cost_usd             NUMERIC(12, 6) DEFAULT 0.0,
    total_cost_usd              NUMERIC(12, 6) DEFAULT 0.0,
    total_cost_inr              NUMERIC(12, 4) DEFAULT 0.0,
    currency                    TEXT NOT NULL DEFAULT 'INR',
    
    -- Raw auditability
    raw_usage_json              JSONB DEFAULT '{}'::jsonb,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_gemini_usage_rec 
    ON isf_recorder.gemini_usage_log (recording_id, created_at ASC);
CREATE INDEX IF NOT EXISTS idx_gemini_usage_monthly 
    ON isf_recorder.gemini_usage_log (request_timestamp, api_status);
CREATE INDEX IF NOT EXISTS idx_gemini_usage_req_id 
    ON isf_recorder.gemini_usage_log (gemini_request_id);
