-- =============================================================
-- 067_view_security_invoker_rls_leak.sql
-- SECURITY FIX — cross-profile RLS leak via a view without security_invoker.
--
-- A Postgres view runs with the VIEW OWNER's privileges by default, so it BYPASSES
-- the row-level security of its base tables unless `security_invoker = true` is set
-- (PG15+). `public.daily_health_summary` was last recreated in mig 060
-- (CREATE OR REPLACE VIEW, line ~353) WITHOUT that option, so it executed as the
-- owner and leaked EVERY profile's rows to any authenticated caller.
--
-- Confirmed under a non-maintainer (Dea, 3eed5503-…) JWT BEFORE this migration:
--   SELECT count(DISTINCT profile_id) FROM whoop_cycles          -> 1   (RLS OK)
--   SELECT count(DISTINCT profile_id) FROM daily_health_summary  -> 2   (LEAK: sees PC)
--   SELECT count(DISTINCT profile_id) FROM daily_supplement_outcomes -> 2 (inherits leak)
-- daily_supplement_outcomes already has security_invoker=true itself, but it reads
-- FROM daily_health_summary, so it inherited the bypass through the dependency.
--
-- Audit of every public view exposing profile_id (pre-fix): all 6 others already
-- carried security_invoker=true; daily_health_summary was the only offender.
--
-- Fix: set security_invoker (RLS of the invoking user applies) + security_barrier
-- (no predicate leaks past the view boundary) on the leaking view and, for
-- defense-in-depth, on the dependent in its chain. View-definition change ONLY —
-- no base-table data is touched.
-- =============================================================

ALTER VIEW public.daily_health_summary
    SET (security_invoker = true, security_barrier = true);

-- Dependent in the chain — already security_invoker=true; add security_barrier too.
ALTER VIEW public.daily_supplement_outcomes
    SET (security_invoker = true, security_barrier = true);

-- ---- structural verification (runs as postgres; asserts the options stuck) -------
DO $$
DECLARE
    v_bad text;
BEGIN
    SELECT string_agg(c.relname, ', ')
      INTO v_bad
      FROM pg_class c
      JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname = 'public'
       AND c.relkind = 'v'
       AND EXISTS (SELECT 1 FROM information_schema.columns col
                    WHERE col.table_schema = 'public'
                      AND col.table_name = c.relname
                      AND col.column_name = 'profile_id')
       AND NOT (coalesce(array_to_string(c.reloptions, ','), '') ILIKE '%security_invoker=true%');
    IF v_bad IS NOT NULL THEN
        RAISE EXCEPTION 'profile_id view(s) still missing security_invoker=true: %', v_bad;
    END IF;
    RAISE NOTICE '067 OK: every public view exposing profile_id now has security_invoker=true';
END $$;
