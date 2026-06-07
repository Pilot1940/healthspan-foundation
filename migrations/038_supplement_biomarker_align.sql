-- Migration 038: supplement + biomarker RPC alignment
--
-- Brings maintainer_ingest_supplement and maintainer_ingest_biomarker into
-- line with maintainer_ingest_food (037):
--   • p_force_stage / p_stage_reason routing (instead of confidence-based)
--   • same guard, same grant posture
--   • stage_reason column in both staging tables
--   • DB-side plausibility gate for biomarkers
--   • COALESCE-safety on NOT NULL staging columns (extracted_name, measured_at/taken_at)
--
-- Root cause of live 400 on maintainer_ingest_supplement (old RPC):
--   a) matched supplement + NULL taken_at → NOT NULL violation on supplement_intake_logs.taken_at
--   b) unmatched supplement + NULL extracted_name → NOT NULL violation on stg_supplement_intake_review
-- Both fixed below (COALESCE + p_force_stage routing).
--
-- Apply:  python3 scripts/hs_ops.py apply migrations/038_supplement_biomarker_align.sql

-- ── 1. stage_reason columns ───────────────────────────────────────────────────

ALTER TABLE public.stg_biomarker_review
  ADD COLUMN IF NOT EXISTS stage_reason text;

COMMENT ON COLUMN public.stg_biomarker_review.stage_reason IS
  'Why this biomarker needs review — set by drain (completeness gate) or RPC (plausibility gate).';

ALTER TABLE public.stg_supplement_intake_review
  ADD COLUMN IF NOT EXISTS stage_reason text;

COMMENT ON COLUMN public.stg_supplement_intake_review.stage_reason IS
  'Why this supplement entry needs review — set by drain (completeness gate) or RPC.';

-- ── 2. maintainer_ingest_supplement ──────────────────────────────────────────

DROP FUNCTION IF EXISTS public.maintainer_ingest_supplement(
  uuid, uuid, numeric, text, timestamptz, text, text, text, numeric, text
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
  v_row_id   uuid;
  v_status   text;
  v_taken_at timestamptz;
BEGIN
  IF NOT (is_maintainer() AND has_profile_access(p_profile_id)) THEN
    RAISE EXCEPTION 'unauthorized: requires maintainer with family access to target profile';
  END IF;

  -- Fall back to now() so the prod INSERT never violates supplement_intake_logs.taken_at NOT NULL
  v_taken_at := COALESCE(p_taken_at, now());

  IF NOT p_force_stage THEN
    -- Upsert on (profile_id, supplement_id, taken_on, source)
    -- taken_on is a plain date column; set it explicitly from taken_at
    INSERT INTO public.supplement_intake_logs (
      profile_id, supplement_id, taken_at, taken_on, source, dose_amount, dose_unit, notes
    ) VALUES (
      p_profile_id, p_supplement_id, v_taken_at,
      (v_taken_at AT TIME ZONE 'UTC')::date,
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
      p_stage_reason
    )
    RETURNING id INTO v_row_id;
    v_status := 'staged';
  END IF;

  RETURN jsonb_build_object(
    'id',           v_row_id,
    'status',       v_status,
    'stage_reason', p_stage_reason
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

-- ── 3. maintainer_ingest_biomarker ────────────────────────────────────────────

DROP FUNCTION IF EXISTS public.maintainer_ingest_biomarker(
  uuid, uuid, numeric, text, timestamptz, text, text, text, numeric, text
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

  -- Caller-supplied reason wins; DB plausibility fills in when caller omitted it
  v_stage_reason := COALESCE(p_stage_reason, v_stage_reason);

  IF NOT v_stage THEN
    -- Upsert on (profile_id, metric_definition_id, measured_at)
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

-- PostgREST must reload schema cache after signature changes
NOTIFY pgrst, 'reload schema';

-- ── 4. Verify ─────────────────────────────────────────────────────────────────
DO $$
DECLARE
  n int;
BEGIN
  -- Old 10-param signatures must be gone
  SELECT COUNT(*) INTO n
  FROM pg_proc p
  JOIN pg_namespace ns ON ns.oid = p.pronamespace
  WHERE ns.nspname = 'public'
    AND p.proname IN ('maintainer_ingest_supplement', 'maintainer_ingest_biomarker')
    AND pronargs = 10;
  ASSERT n = 0, format('old 10-param fn still present (count=%s)', n);

  -- New 12-param signatures must exist (one each)
  SELECT COUNT(*) INTO n
  FROM pg_proc p
  JOIN pg_namespace ns ON ns.oid = p.pronamespace
  WHERE ns.nspname = 'public'
    AND p.proname IN ('maintainer_ingest_supplement', 'maintainer_ingest_biomarker')
    AND pronargs = 12;
  ASSERT n = 2, format('expected 2 new 12-param fns, found %s', n);

  -- stage_reason columns must exist in both staging tables
  SELECT COUNT(*) INTO n
  FROM information_schema.columns
  WHERE table_schema = 'public'
    AND table_name IN ('stg_biomarker_review', 'stg_supplement_intake_review')
    AND column_name = 'stage_reason';
  ASSERT n = 2, format('expected 2 stage_reason columns, found %s', n);

  RAISE NOTICE 'Migration 038 verify: OK — supplement + biomarker RPCs aligned';
END $$;
