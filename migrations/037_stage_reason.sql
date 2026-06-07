-- Migration 037: persist stage_reason on media_inbox and stg_food_log_review
--
-- Every time the drain stages or fails an item it now writes WHY — so the
-- future Telegram review-summary can show the reason alongside each pending item.
--
-- Apply:  python3 scripts/hs_ops.py apply migrations/037_stage_reason.sql

-- ── 1. Add stage_reason column to media_inbox ─────────────────────────────────

ALTER TABLE public.media_inbox
  ADD COLUMN IF NOT EXISTS stage_reason text;

COMMENT ON COLUMN public.media_inbox.stage_reason IS
  'Human-readable reason this item was staged or failed '
  '(e.g. "incomplete: missing calories or macros", '
  '"implausible calories: 12500 kcal (must be 25–12000)", '
  '"vision returned no parseable extraction", '
  '"unknown kind: workout"). Set by the drain at mark_rows time.';

-- ── 2. Add stage_reason column to stg_food_log_review ────────────────────────

ALTER TABLE public.stg_food_log_review
  ADD COLUMN IF NOT EXISTS stage_reason text;

COMMENT ON COLUMN public.stg_food_log_review.stage_reason IS
  'Reason this food entry needs human review — populated by the RPC '
  '(DB-side plausibility gate) or passed from Python (completeness gate, '
  'unknown kind). Used by the daily Telegram review-summary.';

-- ── 3. Rebuild maintainer_ingest_food (adds p_stage_reason param) ─────────────

-- Drop the 15-param (036) version
DROP FUNCTION IF EXISTS public.maintainer_ingest_food(
  uuid, text, text, numeric, numeric, numeric, numeric, numeric,
  timestamptz, text, text, jsonb, numeric, text, boolean
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
  p_stage_reason text        DEFAULT NULL   -- caller-supplied reason; DB may override for kcal gate
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
  v_stage_reason text;   -- DB-computed reason (kcal gate); merged with p_stage_reason below
BEGIN
  -- Auth guard (verbatim from 031) — not auth logic, delegates to existing fns
  IF NOT (is_maintainer() AND has_profile_access(p_profile_id)) THEN
    RAISE EXCEPTION 'unauthorized: requires maintainer with family access to target profile';
  END IF;

  -- Routing: Python completeness gate sets p_force_stage.
  -- DB adds plausibility gate on calories (food_energy_kcal bounds from metric_definitions).
  v_stage := p_force_stage;

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
    -- Plain INSERT to production table — idempotency via media_inbox atomic claim
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

-- ── 4. Grant posture — fresh CREATE gets PUBLIC EXECUTE by default, lock it down ─
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

-- PostgREST must reload its schema cache after a function signature change
NOTIFY pgrst, 'reload schema';

-- ── 5. Verify ─────────────────────────────────────────────────────────────────
DO $$
DECLARE
  n_old  int;
  n_new  int;
  n_mi   int;  -- media_inbox.stage_reason
  n_sfr  int;  -- stg_food_log_review.stage_reason
BEGIN
  -- Old 15-param signature must be gone
  SELECT COUNT(*) INTO n_old
  FROM pg_proc p
  JOIN pg_namespace ns ON ns.oid = p.pronamespace
  WHERE ns.nspname = 'public'
    AND p.proname = 'maintainer_ingest_food'
    AND pronargs = 15;
  ASSERT n_old = 0, format('old 15-param fn still present (count=%s)', n_old);

  -- New 16-param signature must exist
  SELECT COUNT(*) INTO n_new
  FROM pg_proc p
  JOIN pg_namespace ns ON ns.oid = p.pronamespace
  WHERE ns.nspname = 'public'
    AND p.proname = 'maintainer_ingest_food'
    AND pronargs = 16;
  ASSERT n_new = 1, format('new 16-param fn not found (count=%s)', n_new);

  -- media_inbox.stage_reason column must exist
  SELECT COUNT(*) INTO n_mi
  FROM information_schema.columns
  WHERE table_schema = 'public'
    AND table_name   = 'media_inbox'
    AND column_name  = 'stage_reason';
  ASSERT n_mi = 1, 'media_inbox.stage_reason column not found';

  -- stg_food_log_review.stage_reason column must exist
  SELECT COUNT(*) INTO n_sfr
  FROM information_schema.columns
  WHERE table_schema = 'public'
    AND table_name   = 'stg_food_log_review'
    AND column_name  = 'stage_reason';
  ASSERT n_sfr = 1, 'stg_food_log_review.stage_reason column not found';

  RAISE NOTICE 'Migration 037 verify: OK — stage_reason columns added, 16-param fn live';
END $$;
