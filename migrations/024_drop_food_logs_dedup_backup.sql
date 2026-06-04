-- =============================================================
-- 024_drop_food_logs_dedup_backup.sql  (CLEANUP — drop stray backup table)
-- food_logs_dedup_backup_20260602 is a one-off snapshot taken during the 2026-06-02
-- Phuket food_logs de-duplication. The dedup is verified and full pg_dump backups
-- exist (backups/healthspan-fullbackup-*). The table carries 21 rows, RLS is enabled
-- but it has NO policy (default-deny — not part of any read path), and NO other table
-- has an inbound FK referencing it (verified). Safe to drop.
-- =============================================================
BEGIN;

DROP TABLE IF EXISTS public.food_logs_dedup_backup_20260602;

DO $$ BEGIN RAISE NOTICE '024: dropped food_logs_dedup_backup_20260602'; END $$;

COMMIT;
