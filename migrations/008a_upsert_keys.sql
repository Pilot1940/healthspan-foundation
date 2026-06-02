-- =============================================================
-- 008a_upsert_keys.sql  (ADDITIVE — indexes/constraints only)
-- Add natural upsert keys so ingestion can INSERT … ON CONFLICT DO UPDATE
-- idempotently (HealthSpan-Ingestion-Export-Architecture §0).
--
-- PRE-FLIGHT (run before applying):
--   For each proposed key, SELECT duplicate groups — surface any before
--   adding the constraint.  Migration aborts if any exist.
--
-- Decisions (2026-06-02, confirmed by live pre-flight):
--   whoop_cycles      — SKIP: uq_whoop_cycles_profile already exact
--   whoop_workouts    — SKIP: uq_whoop_workouts_profile already exact
--   sprints           — SKIP: uq_sprints_profile_slug already exact
--   whoop_sleeps      — SKIP: keep existing (profile_id, cycle_start, sleep_onset)
--   supplement_regimens — SKIP: existing COALESCE key is stronger (handles NULL start_date)
--   biomarkers        — ADD: (profile_id, metric_definition_id, measured_at)
--   weight_logs       — ADD: (profile_id, logged_at)
--   food_logs         — ADD partial index: (profile_id, source_log_path, log_date, description)
--                        WHERE source_log_path IS NOT NULL
--                        (source_log_path alone had 14 dup groups — one file = many rows)
--
-- Idempotent: CREATE INDEX IF NOT EXISTS / constraint guards.
-- No data mutations. Rollback of this migration = drop the three new objects.
-- =============================================================
BEGIN;

-- ----------------------------------------------------------------
-- PRE-FLIGHT: verify zero duplicate groups under each proposed key.
-- If any table has duplicates the transaction aborts with a clear error.
-- (Never auto-dedupe.)
-- ----------------------------------------------------------------

DO $$
DECLARE
    n_bio  integer;
    n_wt   integer;
    n_food integer;
BEGIN
    -- biomarkers: (profile_id, metric_definition_id, measured_at)
    SELECT count(*) INTO n_bio
    FROM (
        SELECT profile_id, metric_definition_id, measured_at
        FROM public.biomarkers
        GROUP BY profile_id, metric_definition_id, measured_at
        HAVING count(*) > 1
    ) d;

    IF n_bio > 0 THEN
        RAISE EXCEPTION
            'STOP: biomarkers has % duplicate group(s) under (profile_id, metric_definition_id, measured_at). '
            'Inspect and resolve before applying.', n_bio;
    END IF;

    -- weight_logs: (profile_id, logged_at)
    SELECT count(*) INTO n_wt
    FROM (
        SELECT profile_id, logged_at
        FROM public.weight_logs
        GROUP BY profile_id, logged_at
        HAVING count(*) > 1
    ) d;

    IF n_wt > 0 THEN
        RAISE EXCEPTION
            'STOP: weight_logs has % duplicate group(s) under (profile_id, logged_at). '
            'Inspect and resolve before applying.', n_wt;
    END IF;

    -- food_logs: (profile_id, source_log_path, log_date, description)
    --   WHERE source_log_path IS NOT NULL
    SELECT count(*) INTO n_food
    FROM (
        SELECT profile_id, source_log_path, log_date, description
        FROM public.food_logs
        WHERE source_log_path IS NOT NULL
        GROUP BY profile_id, source_log_path, log_date, description
        HAVING count(*) > 1
    ) d;

    IF n_food > 0 THEN
        RAISE EXCEPTION
            'STOP: food_logs has % duplicate group(s) under '
            '(profile_id, source_log_path, log_date, description) WHERE source_log_path IS NOT NULL. '
            'Inspect and resolve before applying.', n_food;
    END IF;

    RAISE NOTICE 'Pre-flight OK: biomarkers=0 dups, weight_logs=0 dups, food_logs=0 dups.';
END $$;

-- ----------------------------------------------------------------
-- 1. biomarkers: unique constraint (profile_id, metric_definition_id, measured_at)
--    One reading per metric per timestamp per profile. Enables upsert in biomarker.py.
-- ----------------------------------------------------------------
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'public.biomarkers'::regclass
          AND conname   = 'uq_biomarkers_profile_metric_time'
    ) THEN
        ALTER TABLE public.biomarkers
            ADD CONSTRAINT uq_biomarkers_profile_metric_time
            UNIQUE (profile_id, metric_definition_id, measured_at);
        RAISE NOTICE 'Created constraint uq_biomarkers_profile_metric_time.';
    ELSE
        RAISE NOTICE 'Constraint uq_biomarkers_profile_metric_time already exists — skipped.';
    END IF;
END $$;

-- ----------------------------------------------------------------
-- 2. weight_logs: unique constraint (profile_id, logged_at)
--    Table is currently empty (0 rows); constraint is safe and future-proofs ingestion.
-- ----------------------------------------------------------------
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'public.weight_logs'::regclass
          AND conname   = 'uq_weight_logs_profile_time'
    ) THEN
        ALTER TABLE public.weight_logs
            ADD CONSTRAINT uq_weight_logs_profile_time
            UNIQUE (profile_id, logged_at);
        RAISE NOTICE 'Created constraint uq_weight_logs_profile_time.';
    ELSE
        RAISE NOTICE 'Constraint uq_weight_logs_profile_time already exists — skipped.';
    END IF;
END $$;

-- ----------------------------------------------------------------
-- 3. food_logs: partial unique index
--    (profile_id, source_log_path, log_date, description) WHERE source_log_path IS NOT NULL
--
--    A plain UNIQUE on source_log_path alone is too coarse (one file → many item rows).
--    This finer key is verified 0 dups across all 124 current rows.
--    Rows without source_log_path (manual skill entries) are excluded — always insert.
-- ----------------------------------------------------------------
CREATE UNIQUE INDEX IF NOT EXISTS uq_food_logs_source_row
    ON public.food_logs (profile_id, source_log_path, log_date, description)
    WHERE source_log_path IS NOT NULL;

DO $$
BEGIN
    RAISE NOTICE 'Created (or already had) partial index uq_food_logs_source_row.';
END $$;

-- ----------------------------------------------------------------
-- Also seed the required system_config key for the ingestion contract
-- (contract.py reads ingest.confidence_min; CLAUDE.md rule #1 forbids hardcoding).
-- Value 0.7 = 70% confidence minimum to write straight to prod.
-- ----------------------------------------------------------------
INSERT INTO public.system_config (key, value, description, category, is_active)
VALUES (
    'ingest.confidence_min',
    '0.7'::jsonb,
    'Minimum confidence score (0–1) to write a resolved record straight to prod; below this → stg_*_review',
    'ingestion',
    true
)
ON CONFLICT (key) DO NOTHING;

DO $$
BEGIN
    RAISE NOTICE 'system_config ingest.confidence_min seeded (or already present).';
END $$;

COMMIT;
