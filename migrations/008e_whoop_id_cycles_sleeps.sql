-- =============================================================
-- 008e_whoop_id_cycles_sleeps.sql  (ADDITIVE)
-- Add whoop_id (WHOOP API internal UUID) to whoop_cycles + whoop_sleeps,
-- mirroring 008b for whoop_workouts. Switch their sync conflict key to whoop_id.
--
-- WHY: cycles keyed on (profile_id, cycle_start) and sleeps on
-- (profile_id, cycle_start, sleep_onset). WHOOP re-scores a sleep/cycle hours
-- after the preliminary version and can SHIFT the onset/start timestamp during
-- finalisation. A shifted timestamp no longer matches the existing row →
-- ON CONFLICT misses → a DUPLICATE row is inserted instead of an overwrite.
-- The WHOOP API returns a stable `id` per cycle and per sleep (already read in
-- whoop_sync.py for joins) — keying on it makes re-scores always overwrite.
--
-- Deep-scan finding (2026-06-03): of 12 timestamp-keyed unique constraints, only
-- the WHOOP API-sourced ones are fragile (source re-issues with shifting
-- timestamps). biomarkers/food_logs/supplement_*/travel/weight/daily_logs/
-- hr_zone_config key on timestamps that are FIXED at source (lab draw instant,
-- chosen date) → re-import reuses the same value → clean upsert. Not touched.
-- whoop_journal_entries (cycle_start, behavior_id) is API-adjacent but CSV-sourced
-- and low-risk — deferred.
--
-- Pattern matches 008b: ADD COLUMN (nullable) → backfill via whoop_sync (script,
-- between steps) → ADD UNIQUE. Old (cycle_start) uniques are KEPT as a secondary
-- guard for any non-API writes (exactly as 008b kept uq_whoop_workouts_profile).
-- Idempotent: IF NOT EXISTS guards throughout.
-- =============================================================

-- Step 1: add the columns (nullable; backfilled by the sync/backfill script)
ALTER TABLE public.whoop_cycles ADD COLUMN IF NOT EXISTS whoop_id TEXT;
ALTER TABLE public.whoop_sleeps ADD COLUMN IF NOT EXISTS whoop_id TEXT;

DO $$ BEGIN RAISE NOTICE '008e step 1: whoop_id columns present on whoop_cycles + whoop_sleeps.'; END $$;

-- Step 2: unique constraints on (profile_id, whoop_id).
-- Safe to add before backfill: PG unique ignores NULLs, so all-NULL columns
-- create no conflicts; once backfilled + synced, whoop_id is the real upsert key.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid='public.whoop_cycles'::regclass AND conname='uq_whoop_cycles_whoop_id'
    ) THEN
        ALTER TABLE public.whoop_cycles
            ADD CONSTRAINT uq_whoop_cycles_whoop_id UNIQUE (profile_id, whoop_id);
        RAISE NOTICE '008e step 2a: uq_whoop_cycles_whoop_id created.';
    ELSE
        RAISE NOTICE '008e step 2a: uq_whoop_cycles_whoop_id already exists — skipped.';
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid='public.whoop_sleeps'::regclass AND conname='uq_whoop_sleeps_whoop_id'
    ) THEN
        ALTER TABLE public.whoop_sleeps
            ADD CONSTRAINT uq_whoop_sleeps_whoop_id UNIQUE (profile_id, whoop_id);
        RAISE NOTICE '008e step 2b: uq_whoop_sleeps_whoop_id created.';
    ELSE
        RAISE NOTICE '008e step 2b: uq_whoop_sleeps_whoop_id already exists — skipped.';
    END IF;
END $$;
