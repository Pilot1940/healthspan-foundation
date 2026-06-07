-- Migration 032: drain service identity + lock down maintainer_ingest RPCs
--
-- 1. family_memberships for healthspan.drain@chitalkar.com
--    drain auth_user_id: 1e2084cf-b1c4-484b-8448-33750c4d8e6c (verified 2026-06-07)
--    Three rows — one per family member profile — role='owner':
--      is_maintainer(drain) = true  (via PC's profile whose is_maintainer=true)
--      has_profile_access(drain, PC)  = true
--      has_profile_access(drain, Dea) = true
--      has_profile_access(drain, Dev) = true
--    → maintainer_ingest_food/biomarker/supplement accepts drain for the whole family.
--    pc.skill@ is untouched (self-scoped to PC only).
--
-- 2. Defense-in-depth REVOKE EXECUTE FROM anon on all three RPCs.
--    Migration 031 revoked FROM PUBLIC but Supabase assigns explicit per-role
--    grants, so anon retained EXECUTE (confirmed via information_schema query).
--    The runtime guard (is_maintainer + has_profile_access) already blocks anon
--    at call time; this REVOKE is a second layer so anon cannot even attempt the call.
--
-- Apply:  python3 scripts/hs_ops.py apply migrations/032_drain_identity.sql
-- Verify: see DO $$ ... $$ block below

-- ── 1. drain family_memberships ───────────────────────────────────────────────

INSERT INTO public.family_memberships (auth_user_id, profile_id, role)
VALUES
  -- PC profile (is_maintainer=true) — gives drain is_maintainer()=true
  ('1e2084cf-b1c4-484b-8448-33750c4d8e6c', '21f69003-46f8-4e1c-a928-b1f694ce4aff', 'owner'),
  -- Dea
  ('1e2084cf-b1c4-484b-8448-33750c4d8e6c', '3eed5503-a26f-4b88-bb76-075208fa5de3', 'owner'),
  -- Dev
  ('1e2084cf-b1c4-484b-8448-33750c4d8e6c', 'd4b7a3fe-6e7a-459f-935d-dfe4dbcfd2b1', 'owner')
ON CONFLICT (auth_user_id, profile_id) DO NOTHING;

-- ── 2. REVOKE EXECUTE FROM anon ───────────────────────────────────────────────
-- Use named-parameter signatures (what pg_get_function_arguments returns) so
-- PostgreSQL resolves the correct overload unambiguously.

REVOKE EXECUTE ON FUNCTION public.maintainer_ingest_food(
  p_profile_id    uuid,
  p_meal_type     text,
  p_description   text,
  p_calories      numeric,
  p_protein_g     numeric,
  p_carbs_g       numeric,
  p_fat_g         numeric,
  p_fiber_g       numeric,
  p_logged_at     timestamp with time zone,
  p_source        text,
  p_notes         text,
  p_foods         jsonb,
  p_confidence    numeric,
  p_raw_text      text
) FROM anon;

REVOKE EXECUTE ON FUNCTION public.maintainer_ingest_biomarker(
  p_profile_id            uuid,
  p_metric_definition_id  uuid,
  p_value                 numeric,
  p_unit                  text,
  p_measured_at           timestamp with time zone,
  p_source                text,
  p_notes                 text,
  p_extracted_name        text,
  p_confidence            numeric,
  p_raw_text              text
) FROM anon;

REVOKE EXECUTE ON FUNCTION public.maintainer_ingest_supplement(
  p_profile_id     uuid,
  p_supplement_id  uuid,
  p_dose_amount    numeric,
  p_dose_unit      text,
  p_taken_at       timestamp with time zone,
  p_source         text,
  p_notes          text,
  p_extracted_name text,
  p_confidence     numeric,
  p_raw_text       text
) FROM anon;

-- ── 3. Verify ─────────────────────────────────────────────────────────────────

DO $$
DECLARE
  drain_uid   uuid := '1e2084cf-b1c4-484b-8448-33750c4d8e6c';
  pc_pid      uuid := '21f69003-46f8-4e1c-a928-b1f694ce4aff';
  dea_pid     uuid := '3eed5503-a26f-4b88-bb76-075208fa5de3';
  dev_pid     uuid := 'd4b7a3fe-6e7a-459f-935d-dfe4dbcfd2b1';

  v_drain_maint     boolean;
  v_drain_pc        boolean;
  v_drain_dea       boolean;
  v_drain_dev       boolean;

  v_fm_count        int;
  v_anon_grants     int;
BEGIN
  -- 3a. family_memberships row count for drain
  SELECT COUNT(*) INTO v_fm_count
  FROM public.family_memberships
  WHERE auth_user_id = drain_uid;
  ASSERT v_fm_count >= 3,
    'Expected ≥ 3 family_memberships for drain, found ' || v_fm_count;

  -- 3b. is_maintainer(drain) — simulates is_maintainer() with drain's uid
  SELECT EXISTS (
    SELECT 1
    FROM public.family_memberships fm
    JOIN public.profiles p ON p.id = fm.profile_id
    WHERE fm.auth_user_id = drain_uid AND p.is_maintainer = true
  ) INTO v_drain_maint;
  ASSERT v_drain_maint,
    'is_maintainer(drain) = false — PC membership missing or PC not is_maintainer';

  -- 3c. has_profile_access for PC / Dea / Dev
  SELECT EXISTS (SELECT 1 FROM public.family_memberships
    WHERE auth_user_id = drain_uid AND profile_id = pc_pid)
  INTO v_drain_pc;
  ASSERT v_drain_pc, 'has_profile_access(drain, PC) = false';

  SELECT EXISTS (SELECT 1 FROM public.family_memberships
    WHERE auth_user_id = drain_uid AND profile_id = dea_pid)
  INTO v_drain_dea;
  ASSERT v_drain_dea, 'has_profile_access(drain, Dea) = false';

  SELECT EXISTS (SELECT 1 FROM public.family_memberships
    WHERE auth_user_id = drain_uid AND profile_id = dev_pid)
  INTO v_drain_dev;
  ASSERT v_drain_dev, 'has_profile_access(drain, Dev) = false';

  -- 3d. anon must have no EXECUTE on any of the three RPCs
  SELECT COUNT(*) INTO v_anon_grants
  FROM information_schema.routine_privileges
  WHERE routine_schema = 'public'
    AND routine_name IN (
      'maintainer_ingest_food',
      'maintainer_ingest_biomarker',
      'maintainer_ingest_supplement'
    )
    AND grantee = 'anon'
    AND privilege_type = 'EXECUTE';
  ASSERT v_anon_grants = 0,
    'anon still has EXECUTE on ' || v_anon_grants || ' maintainer_ingest_* function(s)';

  RAISE NOTICE
    'Migration 032 verify: OK — drain is_maintainer=%, access PC/Dea/Dev=%/%/%, anon EXECUTE grants=%',
    v_drain_maint, v_drain_pc, v_drain_dea, v_drain_dev, v_anon_grants;
END $$;
