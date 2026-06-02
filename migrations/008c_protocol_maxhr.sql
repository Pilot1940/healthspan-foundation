-- =============================================================
-- 008c_protocol_maxhr.sql  (ADDITIVE + DATA FIX)
-- Two independent changes:
--
-- 1. whoop_workouts: ADD COLUMN protocol JSONB (nullable)
--    Stores the intended-session spec for structured workouts.
--    Example: {"type":"4x4","pct_max":[90,95],"rounds":3,"work_min":4,"recovery_min":3}
--    Distinct from the existing `tags` JSONB array (program/label tags).
--    protocol = "what was the plan"; tags = "what program does this belong to".
--
-- 2. hr_zone_config: SET max_hr_bpm = 181 on the 2026-05-03 row.
--    The seeded row had max_hr_bpm = NULL. The Jun-1 workout recorded max_hr 181 bpm,
--    confirming the actual max. Needed for %-max calculations in interval analysis.
-- =============================================================

-- 1. Add protocol column (idempotent)
ALTER TABLE public.whoop_workouts
    ADD COLUMN IF NOT EXISTS protocol JSONB;

DO $$
BEGIN
    RAISE NOTICE '008c step 1: protocol column present on whoop_workouts.';
END $$;

-- 2. Set max_hr_bpm on the 2026-05-03 hr_zone_config row
UPDATE public.hr_zone_config
SET    max_hr_bpm = 181
WHERE  effective_date = '2026-05-03'
  AND  profile_id = (SELECT id FROM profiles WHERE relationship = 'self' LIMIT 1)
  AND  max_hr_bpm IS NULL;

DO $$
DECLARE updated_rows integer;
BEGIN
    GET DIAGNOSTICS updated_rows = ROW_COUNT;
    IF updated_rows = 1 THEN
        RAISE NOTICE '008c step 2: hr_zone_config max_hr_bpm set to 181 on 2026-05-03 row.';
    ELSIF updated_rows = 0 THEN
        RAISE NOTICE '008c step 2: max_hr_bpm already set or row not found — no change.';
    ELSE
        RAISE EXCEPTION '008c step 2: unexpected update count % — check hr_zone_config.', updated_rows;
    END IF;
END $$;
