-- 008_app_version_backup.sql
-- Jomi (2026-10-12): "Update available" prompt (GET /app/latest) and nightly backup status.
-- Idempotent: only inserts missing keys, never overwrites a value an admin already set.
-- Run after 007: psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f 008_app_version_backup.sql

BEGIN;

INSERT INTO isf_recorder.app_config (key, value, description, updated_at)
SELECT k, v, d, NOW() FROM (VALUES
    ('latest_version_code', '3', 'Newest app build served at /apk (Android versionCode)'),
    ('latest_version_name', '1.2.0', 'Newest app version name shown in the update prompt'),
    ('latest_apk_notes', '', 'What is new (shown in the update prompt)'),
    ('min_supported_version_code', '1', 'Builds below this must update (no "Later")'),
    ('notify_test_builds', 'false', 'true = TEST builds (.test) also get the update prompt (test server only)'),
    ('last_backup_at', '', 'Last nightly pg_dump of isf_recorder'),
    ('last_backup_ok', '', 'true/false: dump > 0 bytes and pg_restore --list succeeded'),
    ('last_backup_bytes', '', 'Size of the last dump file')
) AS t(k, v, d)
WHERE NOT EXISTS (SELECT 1 FROM isf_recorder.app_config c WHERE c.key = t.k);

UPDATE isf_recorder.app_config SET value = '008', updated_at = NOW()
 WHERE key = 'schema_version' AND value < '008';

COMMIT;
