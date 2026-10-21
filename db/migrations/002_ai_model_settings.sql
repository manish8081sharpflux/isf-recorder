-- ═══════════════════════════════════════════════════════════════════════════
-- Migration 002: AI Model Settings (Admin-Configurable)
-- ═══════════════════════════════════════════════════════════════════════════
-- Stores the active AI model configuration in organization.settings JSONB.
-- Admin can change the model from the Settings screen without code changes.
-- ═══════════════════════════════════════════════════════════════════════════

-- Seed default AI model settings into ISF organization settings
-- Uses jsonb_set to preserve existing settings while adding ai_models key
UPDATE isf_recorder.organization
SET settings = COALESCE(settings, '{}'::jsonb) || jsonb_build_object(
    'ai_models', jsonb_build_object(
        'transcription_model', 'gemini-2.5-flash',
        'intelligence_model', 'gemini-2.5-flash',
        'updated_at', NOW()::text,
        'updated_by', NULL
    )
)
WHERE slug = 'isf'
  AND NOT (COALESCE(settings, '{}'::jsonb) ? 'ai_models');
