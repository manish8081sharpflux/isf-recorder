-- ═══════════════════════════════════════════════════════════════════════════
-- Migration 003: Transcription runs (compare models on the same audio)
-- ═══════════════════════════════════════════════════════════════════════════
-- Every transcription of a recording is kept as its own "run" with the model
-- used, its transcript, summary and cost, instead of overwriting the previous
-- one. The recording's own transcript fields hold the active run.
-- Also corrects model prices that were wrong in gemini_model_pricing.
-- ═══════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS isf_recorder.transcription_run (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    recording_id      UUID NOT NULL REFERENCES isf_recorder.recording(id) ON DELETE CASCADE,
    model             VARCHAR(100) NOT NULL,
    stt_engine        VARCHAR(30),
    status            VARCHAR(20) NOT NULL DEFAULT 'processing',   -- processing | completed | error
    is_original       BOOLEAN NOT NULL DEFAULT FALSE,               -- snapshot of the transcript made before runs existed
    requested_by      UUID,
    started_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at       TIMESTAMPTZ,
    detected_language VARCHAR(20),
    title             TEXT,
    transcript        TEXT,
    segments          JSONB,
    overview          TEXT,
    key_points        JSONB,
    decisions         JSONB,
    action_items      JSONB,
    error_message     TEXT
);

CREATE INDEX IF NOT EXISTS idx_transcription_run_recording
    ON isf_recorder.transcription_run (recording_id, started_at);

ALTER TABLE isf_recorder.recording
    ADD COLUMN IF NOT EXISTS active_run_id UUID REFERENCES isf_recorder.transcription_run(id) ON DELETE SET NULL;

ALTER TABLE isf_recorder.gemini_usage_log
    ADD COLUMN IF NOT EXISTS run_id UUID REFERENCES isf_recorder.transcription_run(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_gemini_usage_run
    ON isf_recorder.gemini_usage_log (run_id);

-- Gemini 3.5 Flash was priced like 2.5 Flash. Official paid-tier price (ai.google.dev/gemini-api/docs/pricing,
-- checked 2026-10-05): $1.50 / 1M input tokens (all input incl. audio), $9.00 / 1M output incl. thinking.
UPDATE isf_recorder.gemini_model_pricing
SET is_active = FALSE, effective_to = NOW()
WHERE gemini_model = 'gemini-3.5-flash' AND is_active = TRUE AND output_price_per_million <> 9.0000;

INSERT INTO isf_recorder.gemini_model_pricing
    (gemini_model, input_price_per_million, output_price_per_million, audio_price_per_million,
     cached_input_price_per_million, effective_from, pricing_currency, is_active)
SELECT 'gemini-3.5-flash', 1.5000, 9.0000, 1.5000, 0.1500, NOW(), 'USD', TRUE
WHERE NOT EXISTS (
    SELECT 1 FROM isf_recorder.gemini_model_pricing
    WHERE gemini_model = 'gemini-3.5-flash' AND is_active = TRUE
);

-- AssemblyAI is billed per audio hour, not per token: the cost is computed in assemblyai_stt.py.
-- These rows only mark the models as priced (token columns stay 0).
INSERT INTO isf_recorder.gemini_model_pricing
    (gemini_model, input_price_per_million, output_price_per_million, audio_price_per_million,
     cached_input_price_per_million, effective_from, pricing_currency, is_active)
SELECT m, 0, 0, 0, 0, NOW(), 'USD', TRUE
FROM (VALUES ('assemblyai-universal-2'), ('assemblyai-universal-3-5-pro')) AS v(m)
WHERE NOT EXISTS (
    SELECT 1 FROM isf_recorder.gemini_model_pricing p WHERE p.gemini_model = v.m AND p.is_active = TRUE
);

-- The admin dropdown no longer offers models that do not exist on our API keys.
UPDATE isf_recorder.organization
SET settings = jsonb_set(settings, '{ai_models,transcription_model}', '"gemini-2.5-flash"')
WHERE settings->'ai_models'->>'transcription_model' IN ('gemini-2.5-pro', 'gemini-3.0-flash', 'gemini-3.5-pro');

-- Both old AssemblyAI ids always ran Universal-2.
UPDATE isf_recorder.organization
SET settings = jsonb_set(settings, '{ai_models,transcription_model}', '"assemblyai-universal-2"')
WHERE settings->'ai_models'->>'transcription_model' IN ('assemblyai-universal-1', 'assemblyai-nano');

UPDATE isf_recorder.organization
SET settings = jsonb_set(settings, '{ai_models,intelligence_model}', '"gemini-2.5-flash"')
WHERE settings->'ai_models'->>'intelligence_model' IN ('gemini-2.5-pro', 'gemini-3.0-flash', 'gemini-3.5-pro');
