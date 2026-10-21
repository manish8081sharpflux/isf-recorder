-- 009_silence_gaps_openai.sql
-- Jomi v2.1 (2026-10-12): silence markers per recording, and OpenAI transcription prices.
-- Additive and idempotent: safe to run twice; v1.2.0 keeps working.
-- Run after 008: psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f 009_silence_gaps_openai.sql

BEGIN;

-- [{at_voice_sec, wall_start_ms, seconds}] — skipped silence (>= 3 s) at a position of the SAVED audio
ALTER TABLE isf_recorder.recording ADD COLUMN IF NOT EXISTS silence_gaps JSONB DEFAULT '[]'::jsonb;
ALTER TABLE isf_recorder.recording ADD COLUMN IF NOT EXISTS silence_skipped_count INT;

-- ─── OpenAI speech-to-text prices (developers.openai.com/api/docs/pricing, checked 2026-10-12) ──────
-- gpt-4o-transcribe-diarize: per token, $2.50 / 1M input (audio) tokens, $10.00 / 1M output tokens.
-- gpt-transcribe: per audio minute, $0.0045 / min = $0.27 / audio hour (price_per_audio_hour).
INSERT INTO isf_recorder.gemini_model_pricing
    (gemini_model, input_price_per_million, output_price_per_million, audio_price_per_million,
     cached_input_price_per_million, effective_from, effective_to, pricing_currency, is_active, price_per_audio_hour)
SELECT m, i, o, a, 0, TIMESTAMPTZ '2026-10-01 00:00:00+00', NULL, 'USD', TRUE, h
FROM (VALUES
    ('openai-gpt-4o-transcribe-diarize', 2.50, 10.00, 2.50, 0.0),
    ('openai-gpt-transcribe', 0.0, 0.0, 0.0, 0.27)
) AS t(m, i, o, a, h)
WHERE NOT EXISTS (SELECT 1 FROM isf_recorder.gemini_model_pricing p WHERE p.gemini_model = t.m);

UPDATE isf_recorder.app_config SET value = '009', updated_at = NOW()
 WHERE key = 'schema_version' AND value < '009';

COMMIT;
