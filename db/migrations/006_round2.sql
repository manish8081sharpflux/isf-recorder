-- 006_round2.sql
-- Jomi Round 2 (2026-10-09): several assistant coaches per recording, a DB super-admin flag,
-- and the full ISF Balgruh list. Additive and idempotent: safe to run twice; v1.2.0 keeps working.
-- Run after 005: psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f 006_round2.sql

BEGIN;

-- ─── recording: several assistant coaches ([{user_id?, name}]) ──────────────
ALTER TABLE isf_recorder.recording
    ADD COLUMN IF NOT EXISTS assistant_coaches JSONB DEFAULT '[]'::jsonb;

-- Existing single coach text -> one-element list (only rows not filled yet)
UPDATE isf_recorder.recording
   SET assistant_coaches = jsonb_build_array(jsonb_build_object('user_id', NULL, 'name', assistant_coach))
 WHERE assistant_coach IS NOT NULL AND btrim(assistant_coach) <> ''
   AND (assistant_coaches IS NULL OR assistant_coaches = '[]'::jsonb);

-- ─── user: super-admin flag (SUPER_ADMIN_PHONES env stays as the permanent bootstrap) ─
ALTER TABLE isf_recorder."user" ADD COLUMN IF NOT EXISTS is_super_admin BOOLEAN NOT NULL DEFAULT FALSE;
UPDATE isf_recorder."user"
   SET is_super_admin = TRUE, role = 'admin'
 WHERE RIGHT(phone, 10) IN ('9421187857', '9684019697') AND (is_super_admin = FALSE OR role <> 'admin');

-- ─── Balgruh list for ISF: rename the three seeded names, add the rest, fixed order ─
WITH org AS (SELECT id FROM isf_recorder.organization WHERE slug = 'isf'),
renames(old_name, new_name) AS (VALUES
    ('Mohor Boys', 'Mohor Boys, Somwar Peth'),
    ('Mohor Girls', 'Mohor Girls, Budhwar Peth'),
    ('Nirmal Girls', 'Nirmal Girls Balgruh, Kesnand'))
UPDATE isf_recorder.recorder_list_item li
   SET name = r.new_name, updated_at = now()
  FROM renames r, org
 WHERE li.org_id = org.id AND li.list_type = 'balgruh' AND lower(li.name) = lower(r.old_name)
   AND NOT EXISTS (SELECT 1 FROM isf_recorder.recorder_list_item x
                    WHERE x.org_id = org.id AND x.list_type = 'balgruh' AND lower(x.name) = lower(r.new_name));

WITH org AS (SELECT id FROM isf_recorder.organization WHERE slug = 'isf'),
wanted(name, ord) AS (VALUES
    ('Mohor Boys, Somwar Peth', 1),
    ('Mohor Girls, Budhwar Peth', 2),
    ('Nirmal Girls Balgruh, Kesnand', 3),
    ('Nirmal Boys Balgruh, Chakan', 4),
    ('Mauli Balgruh, Wadebolhai', 5),
    ('Matoshree Girls Balgruh, Fursungi', 6),
    ('Sahyadree Balgruh, Manchar', 7),
    ('Matruchaya, Dighi', 8),
    ('Mother Teresa, Chimbli Girls', 9),
    ('Vikas Anath Ashram, Chikhali', 10),
    ('Samaparc Boys, Shelpimpalgaon', 11),
    ('JagatGuru Tukobaraya Warkari Sanstha, Alandi', 12),
    ('Sant Namdev Warkari Sanstha, Alandi', 13),
    ('DhyanPrabodhini Boys Varkari Sanstha, Alandi', 14),
    ('ShriRamkrushna Varkari Sanstha', 15)),
ins AS (
    INSERT INTO isf_recorder.recorder_list_item (org_id, list_type, name, sort_order)
    SELECT org.id, 'balgruh', w.name, w.ord FROM wanted w, org
    ON CONFLICT DO NOTHING
    RETURNING 1)
UPDATE isf_recorder.recorder_list_item li
   SET sort_order = w.ord, updated_at = now()
  FROM wanted w, org
 WHERE li.org_id = org.id AND li.list_type = 'balgruh' AND lower(li.name) = lower(w.name)
   AND li.sort_order <> w.ord;

INSERT INTO isf_recorder.app_config (key, value, description)
VALUES ('schema_version', '006', 'Last applied isf_recorder migration')
ON CONFLICT (key) DO UPDATE SET value = '006', updated_at = now();

COMMIT;
