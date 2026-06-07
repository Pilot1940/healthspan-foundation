-- Migration 040: write-contract audit — fix all RPC→table constraint gaps
--
-- Findings from full cross-check of all three RPCs against target table constraints:
--
-- 1. supplement_intake_logs.source_check missing 'telegram'
--    → any non-staged supplement write from the drain crashes (CHECK violation)
--
-- 2. maintainer_ingest_supplement — no DB-side guard for supplement_id NOT NULL
--    → Python completeness gate is the only protection; add belt-and-suspenders RPC guard
--    → Also: RETURN used p_stage_reason (caller value) not v_stage_reason (may be DB-computed)
--
-- 3. maintainer_ingest_biomarker — no DB-side guards for metric_definition_id/value NOT NULL
--    → Python completeness gate is the only protection; add RPC guards
--
-- 4. maintainer_ingest_food — meal_type CHECK does not include 'unknown'
--    → food vision prompt offers 'unknown' as an option; if AI picks it and food_is_complete()
--      returns True (description+calories+macros present), INSERT crashes (CHECK violation)
--    → Add DB-side guard: stage if meal_type not in valid set
--    → Also: add DB-side guard for description NOT NULL (belt-and-suspenders)
--
-- Apply:  python3 scripts/hs_ops.py apply migrations/040_write_contract_audit.sql

-- ── 1. Fix supplement_intake_logs.source CHECK — add 'telegram' ───────────────

ALTER TABLE public.supplement_intake_logs
  DROP CONSTRAINT supplement_intake_logs_source_check;

ALTER TABLE public.supplement_intake_logs
  ADD CONSTRAINT supplement_intake_logs_source_check
  CHECK (source = ANY (ARRAY[
    'manual'::text, 'journal'::text, 'skill'::text,
    'csv'::text, 'photo'::text, 'telegram'::text
  ]));

-- ── 2. Rebuild maintainer_ingest_supplement (039→040) ────────────────────────
--
-- Changes vs 039:
--   • Use local v_stage / v_stage_reason throughout (mirrors biomarker pattern)
--   • DB-side guard: stage if supplement_id IS NULL (NOT NULL in target table)
--   • RETURN now emits v_stage_reason (was p_stage_reason — missed DB-computed reason)

DROP FUNCTION IF EXISTS public.maintainer_ingest_supplement(
  uuid, uuid, numeric, text, timestamptz, text, text, text, numeric, text, boolean, text
);

CREATE FUNCTION public.maintainer_ingest_supplement(
  p_profile_id     uuid,
  p_supplement_id  uuid        DEFAULT NULL,
  p_dose_amount    numeric     DEFAULT NULL,
  p_dose_unit      text        DEFAULT NULL,
  p_taken_at       timestamptz DEFAULT NULL,
  p_source         text        DEFAULT 'telegram',
  p_notes          text        DEFAULT NULL,
  p_extracted_name text        DEFAULT NULL,
  p_confidence     numeric     DEFAULT 1.0,
  p_raw_text       text        DEFAULT NULL,
  p_force_stage    boolean     DEFAULT FALSE,
  p_stage_reason   text        DEFAULT NULL
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
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
      notes       = EXCLUDED.notes
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
$$;

REVOKE ALL ON FUNCTION public.maintainer_ingest_supplement(
  uuid, uuid, numeric, text, timestamptz, text, text, text, numeric, text, boolean, text
) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.maintainer_ingest_supplement(
  uuid, uuid, numeric, text, timestamptz, text, text, text, numeric, text, boolean, text
) FROM anon;
GRANT EXECUTE ON FUNCTION public.maintainer_ingest_supplement(
  uuid, uuid, numeric, text, timestamptz, text, text, text, numeric, text, boolean, text
) TO authenticated;

-- ── 3. Rebuild maintainer_ingest_biomarker (038→040) ─────────────────────────
--
-- Changes vs 038:
--   • DB-side guard: stage if metric_definition_id IS NULL (NOT NULL in target)
--   • DB-side guard: stage if value IS NULL (NOT NULL in target)
--   (plausibility gate and v_stage/v_stage_reason pattern already present in 038)

DROP FUNCTION IF EXISTS public.maintainer_ingest_biomarker(
  uuid, uuid, numeric, text, timestamptz, text, text, text, numeric, text, boolean, text
);

CREATE FUNCTION public.maintainer_ingest_biomarker(
  p_profile_id           uuid,
  p_metric_definition_id uuid        DEFAULT NULL,
  p_value                numeric     DEFAULT NULL,
  p_unit                 text        DEFAULT NULL,
  p_measured_at          timestamptz DEFAULT NULL,
  p_source               text        DEFAULT 'telegram',
  p_notes                text        DEFAULT NULL,
  p_extracted_name       text        DEFAULT NULL,
  p_confidence           numeric     DEFAULT 1.0,
  p_raw_text             text        DEFAULT NULL,
  p_force_stage          boolean     DEFAULT FALSE,
  p_stage_reason         text        DEFAULT NULL
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
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
      source = EXCLUDED.source
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
$$;

REVOKE ALL ON FUNCTION public.maintainer_ingest_biomarker(
  uuid, uuid, numeric, text, timestamptz, text, text, text, numeric, text, boolean, text
) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.maintainer_ingest_biomarker(
  uuid, uuid, numeric, text, timestamptz, text, text, text, numeric, text, boolean, text
) FROM anon;
GRANT EXECUTE ON FUNCTION public.maintainer_ingest_biomarker(
  uuid, uuid, numeric, text, timestamptz, text, text, text, numeric, text, boolean, text
) TO authenticated;

-- ── 4. Rebuild maintainer_ingest_food (037→040) ──────────────────────────────
--
-- Changes vs 037:
--   • DB-side guard: stage if meal_type not in valid set and not NULL
--     food_logs_meal_type_check = ['breakfast','lunch','dinner','snack','drink','supplement']
--     The food vision prompt offered 'unknown' as an option — that is not in the allowed set.
--     NULL is allowed (passes the CHECK: NULL = ANY(...) evaluates to NULL, not FALSE).
--   • DB-side guard: stage if description IS NULL (NOT NULL in food_logs)
--     Python completeness gate protects this but add belt-and-suspenders.

DROP FUNCTION IF EXISTS public.maintainer_ingest_food(
  uuid, text, text, numeric, numeric, numeric, numeric, numeric,
  timestamptz, text, text, jsonb, numeric, text, boolean, text
);

CREATE FUNCTION public.maintainer_ingest_food(
  p_profile_id   uuid,
  p_meal_type    text,
  p_description  text,
  p_calories     numeric     DEFAULT NULL,
  p_protein_g    numeric     DEFAULT NULL,
  p_carbs_g      numeric     DEFAULT NULL,
  p_fat_g        numeric     DEFAULT NULL,
  p_fiber_g      numeric     DEFAULT NULL,
  p_logged_at    timestamptz DEFAULT now(),
  p_source       text        DEFAULT 'telegram',
  p_notes        text        DEFAULT NULL,
  p_foods        jsonb       DEFAULT NULL,
  p_confidence   numeric     DEFAULT 1.0,
  p_raw_text     text        DEFAULT NULL,
  p_force_stage  boolean     DEFAULT FALSE,
  p_stage_reason text        DEFAULT NULL
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  v_row_id       uuid;
  v_status       text;
  v_stage        boolean;
  v_plo          numeric;
  v_phi          numeric;
  v_stage_reason text;
BEGIN
  IF NOT (is_maintainer() AND has_profile_access(p_profile_id)) THEN
    RAISE EXCEPTION 'unauthorized: requires maintainer with family access to target profile';
  END IF;

  v_stage        := p_force_stage;
  v_stage_reason := p_stage_reason;

  -- DB-side guard: description is NOT NULL in food_logs.
  -- Python food_is_complete() requires description, but guard here as belt-and-suspenders.
  IF NOT v_stage AND p_description IS NULL THEN
    v_stage        := true;
    v_stage_reason := COALESCE(v_stage_reason, 'no description: required for INSERT');
  END IF;

  -- DB-side guard: meal_type CHECK constraint.
  -- food_logs_meal_type_check allows: breakfast, lunch, dinner, snack, drink, supplement.
  -- NULL is allowed (passes CHECK). Invalid non-NULL values (e.g. 'unknown') crash INSERT.
  IF NOT v_stage
     AND p_meal_type IS NOT NULL
     AND p_meal_type NOT IN ('breakfast', 'lunch', 'dinner', 'snack', 'drink', 'supplement')
  THEN
    v_stage        := true;
    v_stage_reason := COALESCE(v_stage_reason,
      format('invalid meal_type: %s', p_meal_type));
  END IF;

  -- DB-side plausibility gate: calories bounds from metric_definitions
  IF NOT v_stage AND p_calories IS NOT NULL THEN
    SELECT plausible_min, plausible_max
    INTO v_plo, v_phi
    FROM public.metric_definitions
    WHERE name = 'food_energy_kcal' AND is_active
    LIMIT 1;

    IF (v_plo IS NOT NULL AND p_calories < v_plo)
    OR (v_phi IS NOT NULL AND p_calories > v_phi) THEN
      v_stage        := true;
      v_stage_reason := format(
        'implausible calories: %s kcal (must be %s–%s)',
        p_calories, v_plo, v_phi
      );
    END IF;
  END IF;

  -- Final reason: caller wins if provided; DB-computed as fallback
  v_stage_reason := COALESCE(p_stage_reason, v_stage_reason);

  IF NOT v_stage THEN
    INSERT INTO public.food_logs (
      profile_id, user_id, meal_type, description, calories, protein_g,
      carbs_g, fat_g, fiber_g, source, notes, foods, logged_at, log_date
    ) VALUES (
      p_profile_id, auth.uid(), p_meal_type, p_description, p_calories, p_protein_g,
      p_carbs_g, p_fat_g, p_fiber_g, p_source, p_notes, p_foods,
      p_logged_at, (p_logged_at AT TIME ZONE 'UTC')::date
    )
    RETURNING id INTO v_row_id;
    v_status := 'inserted';
  ELSE
    INSERT INTO public.stg_food_log_review (
      profile_id, user_id, meal_type, description, calories, protein_g,
      carbs_g, fat_g, fiber_g, foods, confidence, status, raw_text, stage_reason
    ) VALUES (
      p_profile_id, auth.uid(), p_meal_type, p_description, p_calories, p_protein_g,
      p_carbs_g, p_fat_g, p_fiber_g, p_foods, p_confidence, 'pending', p_raw_text,
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
$$;

REVOKE ALL ON FUNCTION public.maintainer_ingest_food(
  uuid, text, text, numeric, numeric, numeric, numeric, numeric,
  timestamptz, text, text, jsonb, numeric, text, boolean, text
) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.maintainer_ingest_food(
  uuid, text, text, numeric, numeric, numeric, numeric, numeric,
  timestamptz, text, text, jsonb, numeric, text, boolean, text
) FROM anon;
GRANT EXECUTE ON FUNCTION public.maintainer_ingest_food(
  uuid, text, text, numeric, numeric, numeric, numeric, numeric,
  timestamptz, text, text, jsonb, numeric, text, boolean, text
) TO authenticated;

NOTIFY pgrst, 'reload schema';

-- ── 5. Verify ─────────────────────────────────────────────────────────────────
DO $$
DECLARE
  v_check text;
  n       int;
BEGIN
  -- supplement source CHECK must include 'telegram'
  SELECT cc.check_clause INTO v_check
  FROM information_schema.table_constraints tc
  JOIN information_schema.check_constraints cc ON cc.constraint_name = tc.constraint_name
  WHERE tc.table_schema = 'public'
    AND tc.table_name   = 'supplement_intake_logs'
    AND tc.constraint_name = 'supplement_intake_logs_source_check';
  ASSERT v_check LIKE '%telegram%',
    format('supplement source CHECK does not include telegram: %s', v_check);

  -- All three RPCs must be 12/12/16 param and exist exactly once each
  SELECT COUNT(*) INTO n FROM pg_proc
  WHERE proname = 'maintainer_ingest_supplement'
    AND pronamespace = 'public'::regnamespace AND pronargs = 12;
  ASSERT n = 1, format('maintainer_ingest_supplement 12-param count=%s', n);

  SELECT COUNT(*) INTO n FROM pg_proc
  WHERE proname = 'maintainer_ingest_biomarker'
    AND pronamespace = 'public'::regnamespace AND pronargs = 12;
  ASSERT n = 1, format('maintainer_ingest_biomarker 12-param count=%s', n);

  SELECT COUNT(*) INTO n FROM pg_proc
  WHERE proname = 'maintainer_ingest_food'
    AND pronamespace = 'public'::regnamespace AND pronargs = 16;
  ASSERT n = 1, format('maintainer_ingest_food 16-param count=%s', n);

  -- supplement RPC body must contain the supplement_id NULL guard
  SELECT prosrc INTO v_check FROM pg_proc
  WHERE proname = 'maintainer_ingest_supplement'
    AND pronamespace = 'public'::regnamespace AND pronargs = 12;
  ASSERT v_check LIKE '%p_supplement_id IS NULL%',
    'supplement RPC missing supplement_id NULL guard';

  -- biomarker RPC body must contain both NULL guards
  SELECT prosrc INTO v_check FROM pg_proc
  WHERE proname = 'maintainer_ingest_biomarker'
    AND pronamespace = 'public'::regnamespace AND pronargs = 12;
  ASSERT v_check LIKE '%p_metric_definition_id IS NULL%',
    'biomarker RPC missing metric_definition_id NULL guard';
  ASSERT v_check LIKE '%p_value IS NULL%',
    'biomarker RPC missing value NULL guard';

  -- food RPC body must contain meal_type guard
  SELECT prosrc INTO v_check FROM pg_proc
  WHERE proname = 'maintainer_ingest_food'
    AND pronamespace = 'public'::regnamespace AND pronargs = 16;
  ASSERT v_check LIKE '%p_meal_type NOT IN%',
    'food RPC missing meal_type guard';
  ASSERT v_check LIKE '%p_description IS NULL%',
    'food RPC missing description NULL guard';

  RAISE NOTICE 'Migration 040 verify: OK — source CHECK fixed, 3 RPCs hardened';
END $$;
