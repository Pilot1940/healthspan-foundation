-- =============================================================
-- 022_maintainer_model.sql  (RLS — maintainer-only SELECT on the maintenance tables)
-- V3-2 / blueprint §4.0. Data-quality, audit, ingestion-health and staging review are
-- a MAINTAINER concern (PC), not a per-user one. Dea never sees this machinery — she
-- gets simple outcomes ("saved" / "flagged for review"). Enforced at the DB:
--   * SELECT on query_audit, wearable_sync_log, wearable_sync_errors, stg_*_review
--     is restricted to the maintainer → a non-maintainer (Dea) session sees 0 rows.
--   * Each person's INGESTION still WRITES its own rows (INSERT/UPDATE by owner), so
--     Dea's food/lab logging keeps working — only SELECT is locked.
--
-- WHY NOT a single ALL policy: SELECT must be maintainer-only while INSERT stays
-- owner-scoped → per-command policies. And: INSERT … RETURNING invokes the SELECT
-- policy (verified), so the contract's sync-log/staging writes were changed to use
-- client-generated UUIDs (no RETURNING). The wearable_sync_errors ALL-policy
-- subqueried wearable_sync_log under the caller's RLS — once sync_log SELECT is
-- maintainer-only that subquery returns 0 for Dea and would block her error INSERTs
-- too; it is replaced with a self-contained INSERT check.
--
-- "maintainer" is PC's normal auth user, flagged — NOT the DB service-role (which is
-- never the skill's connection). "only my user sees the audit" = RLS, not admin grant.
-- =============================================================
BEGIN;

-- 1. Flag the maintainer. PC is the family account owner (relationship='self', the
--    only such profile). Avoids hardcoding a UUID (rule #9).
ALTER TABLE public.profiles
  ADD COLUMN IF NOT EXISTS is_maintainer boolean NOT NULL DEFAULT false;

UPDATE public.profiles SET is_maintainer = true  WHERE relationship = 'self';
UPDATE public.profiles SET is_maintainer = false WHERE relationship <> 'self';

-- 2. Helper mirroring has_profile_access (SECURITY DEFINER + STABLE, reads auth.uid()).
CREATE OR REPLACE FUNCTION public.is_maintainer(p_uid uuid DEFAULT NULL)
RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path TO 'public', 'pg_catalog'
AS $function$
  SELECT EXISTS (
    SELECT 1 FROM profiles
    WHERE auth_user_id = COALESCE(p_uid, auth.uid()) AND is_maintainer = true
  );
$function$;

-- 3. Per-table policies. Drop the old ALL policies, rebuild SELECT=maintainer +
--    owner-scoped writes.

-- wearable_sync_log: SELECT maintainer; INSERT by owner (client-uuid, no RETURNING);
-- direct UPDATE by maintainer only. The owner CLOSES its own run via the SECURITY
-- DEFINER hs_close_sync_log() below — a plain owner UPDATE … WHERE id=… can't work
-- because locating the row reads it, invoking the maintainer-only SELECT policy
-- (a non-maintainer would match 0 rows and never finalise the run).
DROP POLICY IF EXISTS wearable_sync_log_profile_access ON public.wearable_sync_log;
CREATE POLICY wearable_sync_log_sel ON public.wearable_sync_log
  FOR SELECT USING (is_maintainer());
CREATE POLICY wearable_sync_log_ins ON public.wearable_sync_log
  FOR INSERT WITH CHECK (has_profile_access(profile_id));
CREATE POLICY wearable_sync_log_upd ON public.wearable_sync_log
  FOR UPDATE USING (is_maintainer()) WITH CHECK (is_maintainer());

-- Owner-safe run finaliser: updates by id as the function owner (bypasses RLS), so any
-- profile can close its OWN run (it holds the id from open_sync_log) without SELECT
-- access. Mirrors has_profile_access's SECURITY DEFINER style.
CREATE OR REPLACE FUNCTION public.hs_close_sync_log(
    p_id uuid, p_status text, p_records_in int, p_records_upserted int,
    p_records_skipped int, p_records_failed int)
RETURNS void
LANGUAGE sql SECURITY DEFINER
SET search_path TO 'public', 'pg_catalog'
AS $function$
  UPDATE public.wearable_sync_log
     SET status = p_status, records_in = p_records_in, records_upserted = p_records_upserted,
         records_skipped = p_records_skipped, records_failed = p_records_failed,
         completed_at = now()
   WHERE id = p_id;
$function$;
GRANT EXECUTE ON FUNCTION public.hs_close_sync_log(uuid,text,int,int,int,int) TO authenticated;

-- wearable_sync_errors: SELECT maintainer; INSERT self-contained (no profile_id col;
-- must NOT subquery sync_log, which is now maintainer-only → would block Dea's inserts).
DROP POLICY IF EXISTS wearable_sync_errors_access ON public.wearable_sync_errors;
CREATE POLICY wearable_sync_errors_sel ON public.wearable_sync_errors
  FOR SELECT USING (is_maintainer());
CREATE POLICY wearable_sync_errors_ins ON public.wearable_sync_errors
  FOR INSERT WITH CHECK (true);

-- stg_biomarker_review: SELECT maintainer; INSERT by owner (staging from ingestion).
DROP POLICY IF EXISTS stg_biomarker_review_profile_access ON public.stg_biomarker_review;
CREATE POLICY stg_biomarker_review_sel ON public.stg_biomarker_review
  FOR SELECT USING (is_maintainer());
CREATE POLICY stg_biomarker_review_ins ON public.stg_biomarker_review
  FOR INSERT WITH CHECK (has_profile_access(profile_id));

-- stg_food_log_review: SELECT maintainer; INSERT by owner.
DROP POLICY IF EXISTS stg_food_log_review_access ON public.stg_food_log_review;
CREATE POLICY stg_food_log_review_sel ON public.stg_food_log_review
  FOR SELECT USING (is_maintainer());
CREATE POLICY stg_food_log_review_ins ON public.stg_food_log_review
  FOR INSERT WITH CHECK (has_profile_access(profile_id));

-- query_audit (created in 021): tighten SELECT to maintainer; INSERT stays owner-scoped.
DROP POLICY IF EXISTS query_audit_profile_access ON public.query_audit;
CREATE POLICY query_audit_sel ON public.query_audit
  FOR SELECT USING (is_maintainer());
CREATE POLICY query_audit_ins ON public.query_audit
  FOR INSERT WITH CHECK (has_profile_access(profile_id));

DO $$
DECLARE n_maint int;
BEGIN
    SELECT count(*) INTO n_maint FROM public.profiles WHERE is_maintainer;
    RAISE NOTICE '022: % profile(s) flagged maintainer (expect 1 = PC)', n_maint;
    IF n_maint <> 1 THEN
        RAISE EXCEPTION '022: expected exactly 1 maintainer, found %', n_maint;
    END IF;
END $$;

COMMIT;
