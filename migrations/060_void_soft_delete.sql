-- =============================================================
-- 060_void_soft_delete.sql
-- BACKLOG #18: self-correct path — soft-delete (void) instead of DELETE.
--
-- A mis-logged row (e.g. the 2026-06-08 mis-dated NAC duplicate) could not be
-- removed: the write boundary bars DELETE by design, and there was no
-- soft-delete column — so a known-bad row kept counting in every aggregation.
-- Health data stays append-only; corrections are recorded, not erased.
--
-- This migration provides:
--   1. `voided_at timestamptz` + `void_reason text` on the three log tables
--      (supplement_intake_logs, food_logs, biomarkers).
--   2. `maintainer_void_supplement/_food/_biomarker(p_id, p_reason)` —
--      SECURITY DEFINER RPCs, maintainer-gated, working from BOTH connection
--      modes (supabase_client `authenticated` and the definer-RPC path).
--      Idempotent: voiding an already-voided row returns 'already_voided'
--      and preserves the original void timestamp/reason.
--   3. Un-void on re-log: the supplement + biomarker ingest RPCs' ON CONFLICT
--      DO UPDATE now clears voided_at/void_reason — re-logging the same item
--      (same conflict key) means it WAS taken/measured after all; without
--      this the fresh log would land on the voided row and stay invisible.
--   4. `daily_health_summary` view: food_logs subselects exclude voided rows.
--      (Code-side read filters ship in the same commit: brief, drain totals,
--      orphan sweep, analysis, lib/views, plan/goals, contract bounds.)
--   5. REVOKE DELETE ON ALL TABLES FROM healthspan_app — the 2026-06-08
--      manual grant landed on ALL 60+ tables (incl. profiles, system_config,
--      whoop_tokens) and reached the unattended drain; the void RPC is the
--      scoped replacement.
--
-- Apply: python3 scripts/hs_ops.py apply migrations/060_void_soft_delete.sql
-- =============================================================
BEGIN;

-- ── 1. void columns ──

ALTER TABLE public.supplement_intake_logs
  ADD COLUMN IF NOT EXISTS voided_at  timestamptz,
  ADD COLUMN IF NOT EXISTS void_reason text;
ALTER TABLE public.food_logs
  ADD COLUMN IF NOT EXISTS voided_at  timestamptz,
  ADD COLUMN IF NOT EXISTS void_reason text;
ALTER TABLE public.biomarkers
  ADD COLUMN IF NOT EXISTS voided_at  timestamptz,
  ADD COLUMN IF NOT EXISTS void_reason text;

COMMENT ON COLUMN public.supplement_intake_logs.voided_at IS
  'Soft-delete marker (mig 060). Non-NULL = this intake was logged in error and must be excluded from every read/aggregation (WHERE voided_at IS NULL). Set via maintainer_void_supplement(); cleared automatically when the same (profile,supplement,day,source) is re-logged.';
COMMENT ON COLUMN public.supplement_intake_logs.void_reason IS
  'Why the row was voided (mig 060) — audit trail; shows a mistake was corrected rather than erased.';
COMMENT ON COLUMN public.food_logs.voided_at IS
  'Soft-delete marker (mig 060). Non-NULL = excluded from every read/aggregation (WHERE voided_at IS NULL). Set via maintainer_void_food().';
COMMENT ON COLUMN public.food_logs.void_reason IS
  'Why the row was voided (mig 060) — audit trail.';
COMMENT ON COLUMN public.biomarkers.voided_at IS
  'Soft-delete marker (mig 060). Non-NULL = excluded from every read/aggregation (WHERE voided_at IS NULL). Set via maintainer_void_biomarker(); cleared automatically when the same (profile,metric,measured_at) is re-ingested.';
COMMENT ON COLUMN public.biomarkers.void_reason IS
  'Why the row was voided (mig 060) — audit trail.';

-- ── 2. void RPCs (SECURITY DEFINER, maintainer-gated, idempotent) ──

CREATE OR REPLACE FUNCTION public.maintainer_void_supplement(p_id uuid, p_reason text)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO 'public'
AS $function$
DECLARE
  v_profile uuid;
  v_voided  timestamptz;
BEGIN
  IF p_reason IS NULL OR length(trim(p_reason)) < 5 THEN
    RAISE EXCEPTION 'void_reason required (>=5 chars) — the audit trail must say why';
  END IF;
  SELECT profile_id, voided_at INTO v_profile, v_voided
  FROM public.supplement_intake_logs WHERE id = p_id;
  IF v_profile IS NULL THEN
    RETURN jsonb_build_object('id', p_id, 'status', 'not_found');
  END IF;
  IF NOT (is_maintainer() AND has_profile_access(v_profile)) THEN
    RAISE EXCEPTION 'unauthorized: requires maintainer with family access to target profile';
  END IF;
  IF v_voided IS NOT NULL THEN
    RETURN jsonb_build_object('id', p_id, 'status', 'already_voided', 'voided_at', v_voided);
  END IF;
  UPDATE public.supplement_intake_logs
  SET voided_at = now(), void_reason = trim(p_reason)
  WHERE id = p_id;
  RETURN jsonb_build_object('id', p_id, 'status', 'voided');
END;
$function$;

CREATE OR REPLACE FUNCTION public.maintainer_void_food(p_id uuid, p_reason text)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO 'public'
AS $function$
DECLARE
  v_profile uuid;
  v_voided  timestamptz;
BEGIN
  IF p_reason IS NULL OR length(trim(p_reason)) < 5 THEN
    RAISE EXCEPTION 'void_reason required (>=5 chars) — the audit trail must say why';
  END IF;
  SELECT profile_id, voided_at INTO v_profile, v_voided
  FROM public.food_logs WHERE id = p_id;
  IF v_profile IS NULL THEN
    RETURN jsonb_build_object('id', p_id, 'status', 'not_found');
  END IF;
  IF NOT (is_maintainer() AND has_profile_access(v_profile)) THEN
    RAISE EXCEPTION 'unauthorized: requires maintainer with family access to target profile';
  END IF;
  IF v_voided IS NOT NULL THEN
    RETURN jsonb_build_object('id', p_id, 'status', 'already_voided', 'voided_at', v_voided);
  END IF;
  UPDATE public.food_logs
  SET voided_at = now(), void_reason = trim(p_reason)
  WHERE id = p_id;
  RETURN jsonb_build_object('id', p_id, 'status', 'voided');
END;
$function$;

CREATE OR REPLACE FUNCTION public.maintainer_void_biomarker(p_id uuid, p_reason text)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO 'public'
AS $function$
DECLARE
  v_profile uuid;
  v_voided  timestamptz;
BEGIN
  IF p_reason IS NULL OR length(trim(p_reason)) < 5 THEN
    RAISE EXCEPTION 'void_reason required (>=5 chars) — the audit trail must say why';
  END IF;
  SELECT profile_id, voided_at INTO v_profile, v_voided
  FROM public.biomarkers WHERE id = p_id;
  IF v_profile IS NULL THEN
    RETURN jsonb_build_object('id', p_id, 'status', 'not_found');
  END IF;
  IF NOT (is_maintainer() AND has_profile_access(v_profile)) THEN
    RAISE EXCEPTION 'unauthorized: requires maintainer with family access to target profile';
  END IF;
  IF v_voided IS NOT NULL THEN
    RETURN jsonb_build_object('id', p_id, 'status', 'already_voided', 'voided_at', v_voided);
  END IF;
  UPDATE public.biomarkers
  SET voided_at = now(), void_reason = trim(p_reason)
  WHERE id = p_id;
  RETURN jsonb_build_object('id', p_id, 'status', 'voided');
END;
$function$;

-- Mirror the maintainer_ingest_* grant set: authenticated + service_role only.
REVOKE ALL ON FUNCTION public.maintainer_void_supplement(uuid, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.maintainer_void_food(uuid, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.maintainer_void_biomarker(uuid, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.maintainer_void_supplement(uuid, text) TO authenticated, service_role;
GRANT EXECUTE ON FUNCTION public.maintainer_void_food(uuid, text) TO authenticated, service_role;
GRANT EXECUTE ON FUNCTION public.maintainer_void_biomarker(uuid, text) TO authenticated, service_role;

-- ── 3. un-void on re-log (ON CONFLICT DO UPDATE clears the void) ──
-- Full function bodies re-stated from the live definitions (post-040); the ONLY
-- change is the two extra lines in each DO UPDATE SET.

CREATE OR REPLACE FUNCTION public.maintainer_ingest_supplement(p_profile_id uuid, p_supplement_id uuid DEFAULT NULL::uuid, p_dose_amount numeric DEFAULT NULL::numeric, p_dose_unit text DEFAULT NULL::text, p_taken_at timestamp with time zone DEFAULT NULL::timestamp with time zone, p_source text DEFAULT 'telegram'::text, p_notes text DEFAULT NULL::text, p_extracted_name text DEFAULT NULL::text, p_confidence numeric DEFAULT 1.0, p_raw_text text DEFAULT NULL::text, p_force_stage boolean DEFAULT false, p_stage_reason text DEFAULT NULL::text)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public'
AS $function$
DECLARE
  v_row_id       uuid;
  v_status       text;
  v_taken_at     timestamptz;
  v_stage        boolean;
  v_stage_reason text;
BEGIN
  IF NOT (is_maintainer() AND has_profile_access(p_profile_id)) THEN
    RAISE EXCEPTION 'unauthorized: requires maintainer with family access to target profile';
  END IF;

  v_taken_at     := COALESCE(p_taken_at, now());
  v_stage        := p_force_stage;
  v_stage_reason := p_stage_reason;

  -- DB-side guard: supplement_id is NOT NULL in supplement_intake_logs
  -- Python completeness gate should catch this, but guard here so no caller
  -- can produce a cryptic NOT NULL violation by bypassing the gate.
  IF NOT v_stage AND p_supplement_id IS NULL THEN
    v_stage        := true;
    v_stage_reason := COALESCE(v_stage_reason, 'no supplement_id: required for INSERT');
  END IF;

  IF NOT v_stage THEN
    -- taken_on is GENERATED ALWAYS AS ((taken_at AT TIME ZONE 'UTC')::date)
    -- and must NOT appear in the INSERT column list or VALUES; Postgres populates it
    -- automatically from taken_at. It is valid (and required) in the ON CONFLICT target.
    INSERT INTO public.supplement_intake_logs (
      profile_id, supplement_id, taken_at, source, dose_amount, dose_unit, notes
    ) VALUES (
      p_profile_id, p_supplement_id, v_taken_at,
      p_source, p_dose_amount, p_dose_unit, p_notes
    )
    ON CONFLICT (profile_id, supplement_id, taken_on, source)
    DO UPDATE SET
      dose_amount = EXCLUDED.dose_amount,
      dose_unit   = EXCLUDED.dose_unit,
      notes       = EXCLUDED.notes,
      -- mig 060: re-logging the same (profile, supplement, day, source) lands on
      -- this row — a voided row MUST come back, or the new intake stays invisible.
      voided_at   = NULL,
      void_reason = NULL
    RETURNING id INTO v_row_id;
    v_status := 'inserted';
  ELSE
    INSERT INTO public.stg_supplement_intake_review (
      profile_id, supplement_id, extracted_name, extracted_dose, extracted_unit,
      taken_at, confidence, status, raw_text, stage_reason
    ) VALUES (
      p_profile_id,
      p_supplement_id,
      COALESCE(p_extracted_name, 'unknown'),
      p_dose_amount,
      p_dose_unit,
      v_taken_at,
      p_confidence,
      'pending',
      p_raw_text,
      v_stage_reason
    )
    RETURNING id INTO v_row_id;
    v_status := 'staged';
  END IF;

  RETURN jsonb_build_object(
    'id',           v_row_id,
    'status',       v_status,
    'stage_reason', v_stage_reason
  );
END;
$function$;

CREATE OR REPLACE FUNCTION public.maintainer_ingest_biomarker(p_profile_id uuid, p_metric_definition_id uuid DEFAULT NULL::uuid, p_value numeric DEFAULT NULL::numeric, p_unit text DEFAULT NULL::text, p_measured_at timestamp with time zone DEFAULT NULL::timestamp with time zone, p_source text DEFAULT 'telegram'::text, p_notes text DEFAULT NULL::text, p_extracted_name text DEFAULT NULL::text, p_confidence numeric DEFAULT 1.0, p_raw_text text DEFAULT NULL::text, p_force_stage boolean DEFAULT false, p_stage_reason text DEFAULT NULL::text)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public'
AS $function$
DECLARE
  v_row_id       uuid;
  v_status       text;
  v_stage        boolean;
  v_stage_reason text;
  v_measured_at  timestamptz;
  v_plo          numeric;
  v_phi          numeric;
BEGIN
  IF NOT (is_maintainer() AND has_profile_access(p_profile_id)) THEN
    RAISE EXCEPTION 'unauthorized: requires maintainer with family access to target profile';
  END IF;

  v_measured_at  := COALESCE(p_measured_at, now());
  v_stage        := p_force_stage;
  v_stage_reason := p_stage_reason;

  -- DB-side guard: metric_definition_id and value are NOT NULL in biomarkers.
  -- Python completeness gate should catch these, but guard here to prevent
  -- cryptic NOT NULL violations if gate is bypassed.
  IF NOT v_stage AND (p_metric_definition_id IS NULL OR p_value IS NULL) THEN
    v_stage := true;
    v_stage_reason := COALESCE(v_stage_reason,
      CASE
        WHEN p_metric_definition_id IS NULL AND p_value IS NULL
          THEN 'no metric_id and no value: required for INSERT'
        WHEN p_metric_definition_id IS NULL
          THEN 'no metric_id: required for INSERT'
        ELSE
          'no value: required for INSERT'
      END
    );
  END IF;

  -- DB-side plausibility gate: only when metric is resolved and value provided
  IF NOT v_stage AND p_metric_definition_id IS NOT NULL AND p_value IS NOT NULL THEN
    SELECT plausible_min, plausible_max
    INTO v_plo, v_phi
    FROM public.metric_definitions
    WHERE id = p_metric_definition_id AND is_active
    LIMIT 1;

    IF (v_plo IS NOT NULL AND p_value < v_plo)
    OR (v_phi IS NOT NULL AND p_value > v_phi) THEN
      v_stage        := true;
      v_stage_reason := format(
        'implausible value: %s %s (must be %s–%s)',
        p_value, COALESCE(p_unit, ''), v_plo, v_phi
      );
    END IF;
  END IF;

  -- Caller-supplied reason wins; DB-computed fills in when caller omitted it
  v_stage_reason := COALESCE(p_stage_reason, v_stage_reason);

  IF NOT v_stage THEN
    INSERT INTO public.biomarkers (
      profile_id, user_id, metric_definition_id, value, unit,
      source, notes, measured_at
    ) VALUES (
      p_profile_id, auth.uid(), p_metric_definition_id, p_value, p_unit,
      p_source, p_notes, v_measured_at
    )
    ON CONFLICT (profile_id, metric_definition_id, measured_at)
    DO UPDATE SET
      value  = EXCLUDED.value,
      unit   = EXCLUDED.unit,
      notes  = EXCLUDED.notes,
      source = EXCLUDED.source,
      -- mig 060: re-ingesting the same (profile, metric, measured_at) lands on
      -- this row — a voided row MUST come back, or the new reading stays invisible.
      voided_at   = NULL,
      void_reason = NULL
    RETURNING id INTO v_row_id;
    v_status := 'inserted';
  ELSE
    INSERT INTO public.stg_biomarker_review (
      profile_id, user_id, metric_definition_id, extracted_name,
      extracted_value, extracted_unit, measured_at, confidence, status, raw_text,
      stage_reason
    ) VALUES (
      p_profile_id, auth.uid(), p_metric_definition_id,
      COALESCE(p_extracted_name, 'unknown'),
      p_value, p_unit, v_measured_at,
      p_confidence, 'pending', p_raw_text,
      v_stage_reason
    )
    RETURNING id INTO v_row_id;
    v_status := 'staged';
  END IF;

  RETURN jsonb_build_object(
    'id',           v_row_id,
    'status',       v_status,
    'stage_reason', v_stage_reason
  );
END;
$function$;

-- ── 4. daily_health_summary: exclude voided food rows ──
-- Identical to the live definition except the three food_logs subselects gain
-- `AND f.voided_at IS NULL`. Column list unchanged → CREATE OR REPLACE is safe.

CREATE OR REPLACE VIEW public.daily_health_summary AS
 SELECT c.profile_id,
    c.user_id,
    c.cycle_start::date AS date,
    c.recovery_score_pct,
    c.hrv_ms,
    c.resting_hr_bpm,
    c.blood_oxygen_pct,
    c.day_strain,
    c.sleep_performance_pct,
    c.asleep_duration_min,
    c.deep_sws_min,
    c.rem_min,
    c.sleep_efficiency_pct,
    c.sleep_debt_min,
    ( SELECT count(*) AS count
           FROM whoop_workouts w
          WHERE w.cycle_start = c.cycle_start AND w.profile_id = c.profile_id) AS workout_count,
    ( SELECT sum(w.duration_min) AS sum
           FROM whoop_workouts w
          WHERE w.cycle_start = c.cycle_start AND w.profile_id = c.profile_id) AS total_workout_min,
    ( SELECT sum(w.energy_burned_cal) AS sum
           FROM whoop_workouts w
          WHERE w.cycle_start = c.cycle_start AND w.profile_id = c.profile_id) AS total_workout_cal,
    j.had_alcohol,
    j.hydrated_sufficiently,
    j.consumed_added_sugar,
    ( SELECT sum(f.calories) AS sum
           FROM food_logs f
          WHERE f.log_date = c.cycle_start::date AND f.profile_id = c.profile_id
            AND (f.is_day_summary IS NULL OR f.is_day_summary = false)
            AND f.voided_at IS NULL) AS total_calories,
    ( SELECT sum(f.protein_g) AS sum
           FROM food_logs f
          WHERE f.log_date = c.cycle_start::date AND f.profile_id = c.profile_id
            AND (f.is_day_summary IS NULL OR f.is_day_summary = false)
            AND f.voided_at IS NULL) AS total_protein_g,
    ( SELECT sum(f.carbs_g) AS sum
           FROM food_logs f
          WHERE f.log_date = c.cycle_start::date AND f.profile_id = c.profile_id
            AND (f.is_day_summary IS NULL OR f.is_day_summary = false)
            AND f.voided_at IS NULL) AS total_carbs_g,
    s.name AS sprint_name,
    s.slug AS sprint_slug
   FROM whoop_cycles c
     LEFT JOIN whoop_journal j ON j.profile_id = c.profile_id AND j.cycle_start = c.cycle_start
     LEFT JOIN sprints s ON s.id = c.sprint_id
  ORDER BY c.cycle_start DESC;

-- ── 5. revoke the blanket DELETE grant from healthspan_app ──
-- Granted manually 2026-06-08; verification showed it landed on ALL public
-- tables and on the shared role (so the minor's connection + the unattended
-- drain also held DELETE), and it missed the supabase_client path the
-- maintainer skill actually uses. The void RPCs above are the scoped,
-- audited replacement.

REVOKE DELETE ON ALL TABLES IN SCHEMA public FROM healthspan_app;

-- ── verify ──

DO $$
DECLARE
  v_cols int;
  v_fns  int;
  v_del  int;
  v_src  text;
BEGIN
  SELECT count(*) INTO v_cols FROM information_schema.columns
  WHERE table_schema = 'public'
    AND table_name IN ('supplement_intake_logs','food_logs','biomarkers')
    AND column_name IN ('voided_at','void_reason');
  IF v_cols <> 6 THEN
    RAISE EXCEPTION 'Migration 060 verify: expected 6 void columns, found %', v_cols;
  END IF;

  SELECT count(*) INTO v_fns FROM pg_proc
  WHERE pronamespace = 'public'::regnamespace
    AND proname IN ('maintainer_void_supplement','maintainer_void_food','maintainer_void_biomarker');
  IF v_fns <> 3 THEN
    RAISE EXCEPTION 'Migration 060 verify: expected 3 void RPCs, found %', v_fns;
  END IF;

  SELECT count(*) INTO v_del FROM information_schema.role_table_grants
  WHERE table_schema = 'public' AND grantee = 'healthspan_app' AND privilege_type = 'DELETE';
  IF v_del <> 0 THEN
    RAISE EXCEPTION 'Migration 060 verify: healthspan_app still holds % DELETE grants', v_del;
  END IF;

  SELECT prosrc INTO v_src FROM pg_proc WHERE proname = 'maintainer_ingest_supplement';
  ASSERT v_src LIKE '%voided_at   = NULL%', 'supplement ingest RPC missing un-void';
  SELECT prosrc INTO v_src FROM pg_proc WHERE proname = 'maintainer_ingest_biomarker';
  ASSERT v_src LIKE '%voided_at   = NULL%', 'biomarker ingest RPC missing un-void';

  SELECT pg_get_viewdef('public.daily_health_summary'::regclass) INTO v_src;
  ASSERT v_src LIKE '%voided_at IS NULL%', 'daily_health_summary missing voided filter';

  RAISE NOTICE 'Migration 060 verify: OK — void columns + 3 RPCs + un-void-on-relog + view filter + healthspan_app DELETE revoked';
END $$;

COMMIT;
