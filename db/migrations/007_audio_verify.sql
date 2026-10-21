-- 007_audio_verify.sql
-- Jomi (2026-10-12): raw pieces are deleted only after the m4a's content was verified against them.
-- Additive and idempotent: safe to run twice; v1.2.0 keeps working.
-- Run after 006: psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f 007_audio_verify.sql

BEGIN;

ALTER TABLE isf_recorder.recording ADD COLUMN IF NOT EXISTS audio_verified_at TIMESTAMPTZ;
-- ok | rebuilt | failed | rebuild_failed | no_pieces | error
ALTER TABLE isf_recorder.recording ADD COLUMN IF NOT EXISTS audio_verify_status TEXT;

-- The hourly cleanup scans completed recordings whose pieces are still on disk
CREATE INDEX IF NOT EXISTS ix_isfrec_recording_pieces_pending
    ON isf_recorder.recording (created_at)
    WHERE chunks_cleaned_at IS NULL AND status = 'completed';

COMMIT;

-- ─── gemini-3.8-flash prices (ai.google.dev/gemini-api/docs/pricing, checked 2026-10-12) ─────────────
-- Introductory until 2026-12-31: $0.75 in (audio has no separate price -> 0.75), $3.75 out (incl. thinking),
-- $0.075 cached. From 2027-01-01: $1.50 / $7.50 / $0.15. usage_tracker picks the row valid at call time.
BEGIN;
INSERT INTO isf_recorder.gemini_model_pricing
    (gemini_model, input_price_per_million, output_price_per_million, audio_price_per_million,
     cached_input_price_per_million, effective_from, effective_to, pricing_currency, is_active, price_per_audio_hour)
SELECT 'gemini-3.8-flash', 0.75, 3.75, 0.75, 0.075, TIMESTAMPTZ '2026-10-01 00:00:00+00',
       TIMESTAMPTZ '2027-01-01 00:00:00+00', 'USD', TRUE, 0
WHERE NOT EXISTS (SELECT 1 FROM isf_recorder.gemini_model_pricing
                  WHERE gemini_model = 'gemini-3.8-flash' AND effective_from = TIMESTAMPTZ '2026-10-01 00:00:00+00');
INSERT INTO isf_recorder.gemini_model_pricing
    (gemini_model, input_price_per_million, output_price_per_million, audio_price_per_million,
     cached_input_price_per_million, effective_from, effective_to, pricing_currency, is_active, price_per_audio_hour)
SELECT 'gemini-3.8-flash', 1.50, 7.50, 1.50, 0.15, TIMESTAMPTZ '2027-01-01 00:00:00+00', NULL, 'USD', TRUE, 0
WHERE NOT EXISTS (SELECT 1 FROM isf_recorder.gemini_model_pricing
                  WHERE gemini_model = 'gemini-3.8-flash' AND effective_from = TIMESTAMPTZ '2027-01-01 00:00:00+00');
COMMIT;
