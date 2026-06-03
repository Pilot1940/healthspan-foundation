-- =============================================================
-- 020_multiple_concurrent_goals.sql  (ADDITIVE — drop a too-tight unique index)
-- V3-2. uq_user_goals_one_active_per_program enforced ONE active goal per
-- (profile, COALESCE(program_id, zero-uuid)) — so all standalone goals (program_id
-- NULL) collapsed to the same zero-uuid slot and could not coexist. v3 supports
-- MULTIPLE concurrent active goals (standalone + per-program), so drop the index.
-- Progress is tracked per-goal (metric vs target/baseline), not by uniqueness.
-- =============================================================
BEGIN;

DROP INDEX IF EXISTS public.uq_user_goals_one_active_per_program;

-- keep the plain lookup index (idx_user_goals_profile already exists)

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_indexes
               WHERE schemaname='public' AND indexname='uq_user_goals_one_active_per_program') THEN
        RAISE EXCEPTION '020: the one-active-per-program unique index is still present';
    END IF;
    RAISE NOTICE '020: dropped uq_user_goals_one_active_per_program — multiple concurrent active goals allowed';
END $$;

COMMIT;
