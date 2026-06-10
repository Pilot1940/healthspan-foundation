-- =============================================================
-- 062_media_retention_config.sql
-- BACKLOG #7: health-media bucket retention window.
--
-- Photos pile up in the private health-media Storage bucket forever. Once a
-- media_inbox row is done/failed the source image has served its purpose.
-- monitor/media_retention.py (same commit) prunes Storage objects whose
-- media_inbox row is terminal AND older than this window — the DB row is
-- kept for audit with storage_path nulled.
--
-- Rule #1: the window lives in system_config, not code.
--
-- Apply: python3 scripts/hs_ops.py apply migrations/062_media_retention_config.sql
-- =============================================================
BEGIN;

INSERT INTO public.system_config (key, value, description, category, is_active)
VALUES
  ('media.retention_days', '45'::jsonb,
   'Days a health-media Storage object is kept after its media_inbox row reaches a terminal status (done/failed). The retention prune (monitor/media_retention.py, GH Actions cron) deletes older objects and nulls media_inbox.storage_path; the row itself is kept for audit. Rows still pending/processing/staged are never pruned.',
   'media', true)
ON CONFLICT (key) DO UPDATE
  SET description = EXCLUDED.description,
      is_active = true,
      updated_at = now();

DO $$
DECLARE
  v int;
BEGIN
  SELECT count(*) INTO v FROM public.system_config
  WHERE key = 'media.retention_days' AND is_active;
  IF v <> 1 THEN
    RAISE EXCEPTION 'Migration 062 verify: media.retention_days missing';
  END IF;
  RAISE NOTICE 'Migration 062 verify: OK — media.retention_days seeded';
END $$;

COMMIT;
