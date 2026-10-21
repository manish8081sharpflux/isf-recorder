-- 012_balgruha_spelling.sql
-- Spelling fix (16 Oct 2026): "Balgruh" → "Balgruha" in stored names. Only data changes; the
-- identifiers (list_type 'balgruh', recording.balgruh_name) stay as they are so the app keeps working.
-- Idempotent: the regex skips names already spelled "Balgruha".
-- Run: psql -v ON_ERROR_STOP=1 -f 012_balgruha_spelling.sql -d "$DATABASE_URL"

BEGIN;

UPDATE isf_recorder.recorder_list_item li
   SET name = regexp_replace(li.name, '\mBalgruh\M', 'Balgruha', 'g'), updated_at = now()
 WHERE li.list_type = 'balgruh' AND li.name ~ '\mBalgruh\M'
   AND NOT EXISTS (SELECT 1 FROM isf_recorder.recorder_list_item x
                    WHERE x.org_id = li.org_id AND x.list_type = 'balgruh'
                      AND lower(x.name) = lower(regexp_replace(li.name, '\mBalgruh\M', 'Balgruha', 'g')));

UPDATE isf_recorder.recording
   SET balgruh_name = regexp_replace(balgruh_name, '\mBalgruh\M', 'Balgruha', 'g')
 WHERE balgruh_name ~ '\mBalgruh\M';

UPDATE isf_recorder.recording
   SET title = regexp_replace(title, '\mBalgruh\M', 'Balgruha', 'g')
 WHERE title ~ '\mBalgruh\M';

UPDATE isf_recorder.app_config SET value = '012', updated_at = NOW()
 WHERE key = 'schema_version' AND value < '012';

COMMIT;
