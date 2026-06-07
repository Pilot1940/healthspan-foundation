-- Migration 039: fix supplement RPC — remove taken_on from INSERT
--
-- supplement_intake_logs.taken_on is GENERATED ALWAYS AS
--   ((taken_at AT TIME ZONE 'UTC'::text))::date
-- Inserting it explicitly causes PostgreSQL error 428C9
-- ("cannot insert into a generated column"). PostgreSQL populates it
-- automatically from taken_at, so it must be absent from the column list
-- AND the VALUES clause. It is valid (and required) in the ON CONFLICT target.
--
-- No change to column signatures, return shape, or grants — pure SQL fix.
--
-- Apply:  python3 scripts/hs_ops.py apply migrations/039_supplement_rpc_fix_generated_taken_on.sql

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
  v_row_id   uuid;
  v_status   text;
  v_taken_at timestamptz;
BEGIN
  IF NOT (is_maintainer() AND has_profile_access(p_profile_id)) THEN
    RAISE EXCEPTION 'unauthorized: requires maintainer with family access to target profile';
  END IF;

  -- Fall back to now() so the INSERT never violates taken_at NOT NULL
  v_taken_at := COALESCE(p_taken_at, now());

  IF NOT p_force_stage THEN
    -- taken_on is GENERATED ALWAYS AS ((taken_at AT TIME ZONE 'UTC')::date)
    -- and must NOT appear in the INSERT column list or VALUES; Postgres populates it
    -- automatically from taken_at. It is valid as an ON CONFLICT target.
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

NOTIFY pgrst, 'reload schema';

-- ── Verify ────────────────────────────────────────────────────────────────────
DO $$
DECLARE
  src text;
BEGIN
  SELECT prosrc INTO src
  FROM pg_proc
  WHERE proname = 'maintainer_ingest_supplement'
    AND pronamespace = 'public'::regnamespace
    AND pronargs = 12;

  ASSERT src IS NOT NULL, 'maintainer_ingest_supplement 12-param not found';

  -- Column list must have taken_at followed by source (no taken_on slotted between them)
  ASSERT src LIKE '%taken_at, source%',
    'INSERT column list: expected "taken_at, source" adjacent — taken_on must not be between them';

  -- taken_on must still be in the ON CONFLICT target
  ASSERT src LIKE '%ON CONFLICT%taken_on%',
    'taken_on missing from ON CONFLICT target';

  RAISE NOTICE 'Migration 039 verify: OK — taken_on removed from INSERT, kept in ON CONFLICT';
END $$;
