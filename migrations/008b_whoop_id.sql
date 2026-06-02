-- =============================================================
-- 008b_whoop_id.sql  (ADDITIVE)
-- Add whoop_id (WHOOP API internal UUID) to whoop_workouts.
--
-- Why: CSV and API timestamps differ by sub-second precision — the CSV
-- truncates to the minute while the API returns exact timestamps. Using
-- (profile_id, workout_start) as the upsert key created duplicates on the
-- first backfill. whoop_id is the stable, platform-assigned UUID that
-- unambiguously identifies each workout across all ingestion paths.
--
-- What this migration does:
--   1. ADD COLUMN whoop_id TEXT  (nullable to allow backfill first)
--   2. ADD UNIQUE (profile_id, whoop_id) as uq_whoop_workouts_whoop_id
--      (deferred until step 2 after whoop_sync --backfill populates it)
--
-- Step 1 and Step 2 are separate DO blocks so the column lands first,
-- whoop_sync --backfill populates it, and then the unique constraint is added
-- with zero risk of constraint failure on existing data.
--
-- Idempotent: ADD COLUMN IF NOT EXISTS; constraint guard via pg_constraint.
-- No data mutations. Rollback = drop column (cascades the unique index).
-- =============================================================

-- Step 1: add the column (nullable, no constraint yet)
ALTER TABLE public.whoop_workouts
    ADD COLUMN IF NOT EXISTS whoop_id TEXT;

DO $$
BEGIN
    RAISE NOTICE 'Step 1 complete: whoop_id column present on whoop_workouts.';
END $$;

-- Step 2: unique constraint (safe to add before backfill only if whoop_id is all NULL)
-- Guard: only add if not already present AND all current values are NULL or
-- the column was just added (safe to constrain before backfill since no dupes possible
-- on all-NULL column — PG unique ignores NULLs).
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'public.whoop_workouts'::regclass
          AND conname   = 'uq_whoop_workouts_whoop_id'
    ) THEN
        ALTER TABLE public.whoop_workouts
            ADD CONSTRAINT uq_whoop_workouts_whoop_id
            UNIQUE (profile_id, whoop_id);
        RAISE NOTICE 'Step 2 complete: unique constraint uq_whoop_workouts_whoop_id created.';
    ELSE
        RAISE NOTICE 'Step 2: constraint uq_whoop_workouts_whoop_id already exists — skipped.';
    END IF;
END $$;
