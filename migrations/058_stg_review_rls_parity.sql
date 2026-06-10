-- =============================================================
-- 058_stg_review_rls_parity.sql
-- Three fixes surfaced by the 2026-06-10 deep scan:
--
-- 1. RLS parity for the staging review tables migration 022 MISSED.
--    Mig 022's header claims maintainer-only SELECT on all stg_*_review,
--    but its body only rebuilt stg_biomarker_review + stg_food_log_review.
--    stg_supplement_intake_review (live, 8 rows), stg_food_rule_review and
--    stg_test_result_review (dormant) still carried the mig-004 ALL /
--    has_profile_access(profile_id) policy — so a non-maintainer family
--    member could SELECT (and UPDATE) her own staged review rows, breaking
--    the "sees outcomes, never the machinery" model (SYSTEM.md §4.3,
--    CLAUDE.md maintainer model). Rebuild them to the mig-022 pattern:
--    SELECT = is_maintainer(); INSERT = has_profile_access(profile_id).
--    (Writers are unaffected: the drain stages via SECURITY DEFINER
--    maintainer_ingest_* RPCs; telegram-webhook retires rows as
--    service_role, which bypasses RLS.)
--
-- 2. Seed `brief.dedup_sec` (cross-run window for collapsing burst auto
--    post-log briefs, commits bc672c9/d2d5d3d). The code reads
--    cfg.get('brief.dedup_sec', 600) but the key was never seeded, so the
--    hardcoded fallback governed behaviour — against rule #1 (all
--    thresholds from system_config).
--
-- 3. Version the out-of-band `rls_auto_enable` event-trigger function.
--    It exists live (event trigger `ensure_rls`: auto-enables RLS on any
--    new public table) but appeared in no migration — created ad hoc in
--    the SQL editor. CREATE OR REPLACE the identical definition + ensure
--    the event trigger, so the repo is the source of truth again.
--
-- Apply: python3 scripts/hs_ops.py apply migrations/058_stg_review_rls_parity.sql
-- =============================================================
BEGIN;

-- ── 1. stg_*_review RLS parity (mirror mig 022's food/biomarker pattern) ──

DROP POLICY IF EXISTS stg_supplement_intake_review_access ON public.stg_supplement_intake_review;
CREATE POLICY stg_supplement_intake_review_sel ON public.stg_supplement_intake_review
  FOR SELECT USING (is_maintainer());
CREATE POLICY stg_supplement_intake_review_ins ON public.stg_supplement_intake_review
  FOR INSERT WITH CHECK (has_profile_access(profile_id));

DROP POLICY IF EXISTS stg_food_rule_review_profile_access ON public.stg_food_rule_review;
CREATE POLICY stg_food_rule_review_sel ON public.stg_food_rule_review
  FOR SELECT USING (is_maintainer());
CREATE POLICY stg_food_rule_review_ins ON public.stg_food_rule_review
  FOR INSERT WITH CHECK (has_profile_access(profile_id));

DROP POLICY IF EXISTS stg_test_result_review_profile_access ON public.stg_test_result_review;
CREATE POLICY stg_test_result_review_sel ON public.stg_test_result_review
  FOR SELECT USING (is_maintainer());
CREATE POLICY stg_test_result_review_ins ON public.stg_test_result_review
  FOR INSERT WITH CHECK (has_profile_access(profile_id));

-- ── 2. brief.dedup_sec seed ──

INSERT INTO public.system_config (key, value, description, category, is_active)
VALUES (
  'brief.dedup_sec',
  '600'::jsonb,
  'Window (seconds) for collapsing burst AUTO post-log briefs across drain runs (push_log dedup). Explicit brief requests always send.',
  'brief',
  true
)
ON CONFLICT (key) DO UPDATE
  SET description = EXCLUDED.description,
      is_active = true,
      updated_at = now();

-- ── 3. Version rls_auto_enable + ensure_rls (identical to live definition) ──

CREATE OR REPLACE FUNCTION public.rls_auto_enable()
 RETURNS event_trigger
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog'
AS $function$
DECLARE
  cmd record;
BEGIN
  FOR cmd IN
    SELECT *
    FROM pg_event_trigger_ddl_commands()
    WHERE command_tag IN ('CREATE TABLE', 'CREATE TABLE AS', 'SELECT INTO')
      AND object_type IN ('table','partitioned table')
  LOOP
     IF cmd.schema_name IS NOT NULL AND cmd.schema_name IN ('public') AND cmd.schema_name NOT IN ('pg_catalog','information_schema') AND cmd.schema_name NOT LIKE 'pg_toast%' AND cmd.schema_name NOT LIKE 'pg_temp%' THEN
      BEGIN
        EXECUTE format('alter table if exists %s enable row level security', cmd.object_identity);
        RAISE LOG 'rls_auto_enable: enabled RLS on %', cmd.object_identity;
      EXCEPTION
        WHEN OTHERS THEN
          RAISE LOG 'rls_auto_enable: failed to enable RLS on %', cmd.object_identity;
      END;
     ELSE
        RAISE LOG 'rls_auto_enable: skip % (either system schema or not in enforced list: %.)', cmd.object_identity, cmd.schema_name;
     END IF;
  END LOOP;
END;
$function$;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_event_trigger WHERE evtname = 'ensure_rls') THEN
    CREATE EVENT TRIGGER ensure_rls
      ON ddl_command_end
      WHEN TAG IN ('CREATE TABLE', 'CREATE TABLE AS', 'SELECT INTO')
      EXECUTE FUNCTION public.rls_auto_enable();
  END IF;
END $$;

-- ── verify ──

DO $$
DECLARE
  v_bad int;
BEGIN
  SELECT count(*) INTO v_bad
  FROM pg_policies
  WHERE schemaname = 'public'
    AND tablename IN ('stg_supplement_intake_review','stg_food_rule_review','stg_test_result_review')
    AND NOT (
      (cmd = 'SELECT' AND qual = 'is_maintainer()')
      OR (cmd = 'INSERT' AND with_check = 'has_profile_access(profile_id)')
    );
  IF v_bad > 0 THEN
    RAISE EXCEPTION 'Migration 058 verify: % unexpected stg_*_review policies remain', v_bad;
  END IF;

  IF NOT EXISTS (SELECT 1 FROM public.system_config WHERE key = 'brief.dedup_sec' AND is_active) THEN
    RAISE EXCEPTION 'Migration 058 verify: brief.dedup_sec not seeded';
  END IF;

  IF NOT EXISTS (SELECT 1 FROM pg_event_trigger WHERE evtname = 'ensure_rls') THEN
    RAISE EXCEPTION 'Migration 058 verify: ensure_rls event trigger missing';
  END IF;

  RAISE NOTICE 'Migration 058 verify: OK';
END $$;

COMMIT;
