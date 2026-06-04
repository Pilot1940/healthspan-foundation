-- =============================================================
-- 027_supplement_intake_source_photo.sql  (CHECK — widen source vocab)
-- The method/source audit missed supplement_intake_logs: its source CHECK allowed only
-- ('manual','journal','skill','csv'), so a photo-sourced intake (parsed from a supplement
-- label / pill-tray photo) could not be written — food_logs and biomarkers already accept
-- 'photo'. Add 'photo' to align the three. Forward-compatible: every existing value stays
-- valid, so no row needs rewriting (today's creatine 6g Jun-4 row logged as 'manual' as a
-- workaround is fine to leave).
-- =============================================================
BEGIN;

ALTER TABLE public.supplement_intake_logs
  DROP CONSTRAINT IF EXISTS supplement_intake_logs_source_check;

ALTER TABLE public.supplement_intake_logs
  ADD CONSTRAINT supplement_intake_logs_source_check
  CHECK (source = ANY (ARRAY['manual', 'journal', 'skill', 'csv', 'photo']::text[]));

DO $$ BEGIN RAISE NOTICE '027: supplement_intake_logs.source now allows photo'; END $$;

COMMIT;
