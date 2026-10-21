-- ═══════════════════════════════════════════════════════════════════════════
-- Migration 004: Per-audio-hour prices in the pricing table
-- ═══════════════════════════════════════════════════════════════════════════
-- AssemblyAI bills per audio hour, not per token. All prices now live in
-- gemini_model_pricing (no prices in code); admins change them with
-- POST /admin/gemini-usage/pricing.
-- ═══════════════════════════════════════════════════════════════════════════

ALTER TABLE isf_recorder.gemini_model_pricing
    ADD COLUMN IF NOT EXISTS price_per_audio_hour NUMERIC(10, 4) NOT NULL DEFAULT 0;

-- assemblyai.com/pricing (checked 2026-10-05): Universal-3.5 Pro $0.21/h, Universal-2 $0.15/h,
-- plus $0.02/h for speaker labels (always on in ISF).
UPDATE isf_recorder.gemini_model_pricing SET price_per_audio_hour = 0.2300
WHERE gemini_model = 'assemblyai-universal-3-5-pro' AND is_active = TRUE;

UPDATE isf_recorder.gemini_model_pricing SET price_per_audio_hour = 0.1700
WHERE gemini_model = 'assemblyai-universal-2' AND is_active = TRUE;
