-- =============================================================
-- 014_healthspan_app_role.sql  (ROLE — Access Model A, path A2)
-- PROMPT 14. A restricted LOGIN role for the direct-psycopg2 path (PC local).
--
-- Design: rather than duplicating 010's grants + re-creating every RLS policy for a
-- new role, healthspan_app is a LOGIN role that is a MEMBER of `authenticated`.
-- At session start the app runs:  SET ROLE authenticated;  + sets request.jwt.claims
-- → it then has EXACTLY the privileges 010 gave authenticated (SELECT/INSERT/UPDATE,
-- no DELETE/TRUNCATE, catalogs SELECT-only) AND the RLS policies (which are scoped
-- TO authenticated) apply, with has_profile_access() resolving via the JWT claim.
-- NOINHERIT means the role does NOT silently inherit authenticated — it must SET ROLE
-- explicitly, so a bare healthspan_app connection that forgets the claim sees nothing.
--
-- PASSWORD is NOT set here (would leak into git). It is set by the apply wrapper
-- (generated, written to gitignored config/healthspan_app.secret.txt).
-- =============================================================
BEGIN;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='healthspan_app') THEN
    CREATE ROLE healthspan_app LOGIN NOINHERIT NOCREATEDB NOCREATEROLE NOSUPERUSER NOBYPASSRLS;
    RAISE NOTICE '014: role healthspan_app created (LOGIN, NOINHERIT, no DDL, no RLS bypass).';
  ELSE
    RAISE NOTICE '014: role healthspan_app already exists — skipped create.';
  END IF;
END $$;

-- Membership in authenticated → inherits 010's grants + RLS policies via SET ROLE.
GRANT authenticated TO healthspan_app;
GRANT USAGE ON SCHEMA public TO healthspan_app;

-- Defence in depth: explicitly deny DELETE/TRUNCATE to the login role itself too
-- (so even without SET ROLE it can never destroy).
DO $$
DECLARE t text;
BEGIN
  FOR t IN SELECT tablename FROM pg_tables WHERE schemaname='public' LOOP
    EXECUTE format('REVOKE DELETE, TRUNCATE ON public.%I FROM healthspan_app', t);
  END LOOP;
END $$;

COMMIT;
