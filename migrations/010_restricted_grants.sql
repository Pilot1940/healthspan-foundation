-- =============================================================
-- 010_restricted_grants.sql  (GRANTS — Access Model A hardening)
-- PROMPT 10. The skill authenticates as Supabase `authenticated` (per-person
-- auth user → JWT → RLS via has_profile_access). Harden that role so the skill
-- can never DELETE/TRUNCATE and can never write the shared catalogs.
--
-- In Supabase, all per-person auth users share the `authenticated` Postgres role;
-- RLS (has_profile_access) is what scopes each person to their own profile_id.
-- So "restricted healthspan_app role" = hardened grants on `authenticated`.
-- `anon` (unauthenticated) gets ZERO table access in founder phase — function-level
-- grants (e.g. the ingest RPC) are untouched (this only revokes TABLE privileges).
--
-- Defence in depth: even a query missing a WHERE clause can't leak (RLS) and
-- can't destroy (no DELETE/TRUNCATE).
-- =============================================================
BEGIN;

-- 1. Revoke destructive privileges from authenticated; revoke ALL table privs from anon.
DO $$
DECLARE t text;
BEGIN
  FOR t IN SELECT tablename FROM pg_tables WHERE schemaname='public' LOOP
    EXECUTE format('REVOKE DELETE, TRUNCATE ON public.%I FROM authenticated', t);
    EXECUTE format('REVOKE ALL PRIVILEGES ON public.%I FROM anon', t);
  END LOOP;
  RAISE NOTICE '010: revoked DELETE/TRUNCATE from authenticated + ALL from anon on all public tables.';
END $$;

-- 2. Shared catalogs: authenticated keeps SELECT only (INSERT/UPDATE is owner-only,
--    done via service_role/postgres). Skill flags an unknown metric/food/supplement
--    for the owner instead of writing the catalog (prevention rule #3).
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY[
    'metric_definitions','food_guidance','supplements','journal_behaviors','hr_zone_config',
    'supplement_aliases','journal_behavior_aliases','loinc_reference','supplement_components',
    'test_definitions','test_targets','log_type_config','source_priority_config','food_rules',
    'biomarker_targets','canonical_aliases'
  ] LOOP
    IF EXISTS (SELECT 1 FROM pg_tables WHERE schemaname='public' AND tablename=t) THEN
      EXECUTE format('REVOKE INSERT, UPDATE ON public.%I FROM authenticated', t);
    END IF;
  END LOOP;
  RAISE NOTICE '010: catalogs are now SELECT-only for authenticated.';
END $$;

-- 3. Belt-and-braces: default privileges for future tables created by postgres
--    do NOT auto-grant DELETE to authenticated. (Supabase grants broadly by default;
--    this narrows new objects.)
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  REVOKE DELETE, TRUNCATE ON TABLES FROM authenticated;

DO $$ BEGIN RAISE NOTICE '010 complete.'; END $$;
COMMIT;
