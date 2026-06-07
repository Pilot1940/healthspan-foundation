-- Migration 036: completeness-based food ingest gating
--
-- Changes:
--   1. Remove duplicate system_config key ingest.confidence_min
--      (lib/contract.confidence_min() now reads ingest.confidence_threshold — same value).
--   2. Replace maintainer_ingest_food with a version that accepts p_force_stage.
--      Routing is now controlled by the Python completeness gate, not model confidence.
--      DB adds a second plausibility gate on calories via metric_definitions.food_energy_kcal.
--   3. NOTIFY pgrst so PostgREST refreshes its schema cache.
--
-- Apply: python scripts/hs_ops.py apply migrations/036_completeness_gate.sql

-- ── 1. Collapse duplicate config key ──────────────────────────────────────────
DELETE FROM public.system_config WHERE key = 'ingest.confidence_min';

-- ── 2. Drop old 14-param signature ────────────────────────────────────────────
-- CREATE OR REPLACE would create a new overload, not replace it, because the
-- parameter list changed. We must DROP first so PostgREST sees only one variant.
DROP FUNCTION IF EXISTS public.maintainer_ingest_food(
  uuid, text, text, numeric, numeric, numeric, numeric, numeric,
  timestamptz, text, text, jsonb, numeric, text
);

-- ── 3. Create new 15-param version (adds p_force_stage) ──────────────────────
CREATE FUNCTION public.maintainer_ingest_food(
  p_profile_id  uuid,
  p_meal_type   text,
  p_description text,
  p_calories    numeric     DEFAULT NULL,
  p_protein_g   numeric     DEFAULT NULL,
  p_carbs_g     numeric     DEFAULT NULL,
  p_fat_g       numeric     DEFAULT NULL,
  p_fiber_g     numeric     DEFAULT NULL,
  p_logged_at   timestamptz DEFAULT now(),
  p_source      text        DEFAULT 'telegram',
  p_notes       text        DEFAULT NULL,
  p_foods       jsonb       DEFAULT NULL,
  p_confidence  numeric     DEFAULT 1.0,
  p_raw_text    text        DEFAULT NULL,
  p_force_stage boolean     DEFAULT FALSE
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  v_row_id    uuid;
  v_status    text;
  v_stage     boolean;
  v_plo       numeric;
  v_phi       numeric;
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
      v_stage := true;
    END IF;
  END IF;

  IF NOT v_stage THEN
    -- Plain INSERT — no ON CONFLICT. Telegram entries have no source_log_path so
    -- the partial unique index (uq_food_logs_source_row WHERE source_log_path IS NOT NULL)
    -- does not apply. Idempotency guaranteed upstream by media_inbox atomic claim.
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
      carbs_g, fat_g, fiber_g, foods, confidence, status, raw_text
    ) VALUES (
      p_profile_id, auth.uid(), p_meal_type, p_description, p_calories, p_protein_g,
      p_carbs_g, p_fat_g, p_fiber_g, p_foods, p_confidence, 'pending', p_raw_text
    )
    RETURNING id INTO v_row_id;
    v_status := 'staged';
  END IF;

  RETURN jsonb_build_object('id', v_row_id, 'status', v_status);
END;
$$;

-- ── 4. Grant posture — fresh CREATE gets PUBLIC EXECUTE by default, lock it down ─
REVOKE ALL ON FUNCTION public.maintainer_ingest_food(
  uuid, text, text, numeric, numeric, numeric, numeric, numeric,
  timestamptz, text, text, jsonb, numeric, text, boolean
) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.maintainer_ingest_food(
  uuid, text, text, numeric, numeric, numeric, numeric, numeric,
  timestamptz, text, text, jsonb, numeric, text, boolean
) FROM anon;
GRANT EXECUTE ON FUNCTION public.maintainer_ingest_food(
  uuid, text, text, numeric, numeric, numeric, numeric, numeric,
  timestamptz, text, text, jsonb, numeric, text, boolean
) TO authenticated;

-- ── 5. Refresh PostgREST schema cache ─────────────────────────────────────────
NOTIFY pgrst, 'reload schema';

-- ── 6. Verify ─────────────────────────────────────────────────────────────────
DO $$
DECLARE
  n_old   int;
  n_new   int;
  n_cmin  int;
BEGIN
  -- Old 14-param signature must be gone
  SELECT count(*) INTO n_old
  FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
  WHERE n.nspname = 'public'
    AND p.proname = 'maintainer_ingest_food'
    AND p.pronargs = 14;
  ASSERT n_old = 0, '036: old 14-param maintainer_ingest_food still present';

  -- New 15-param signature must exist
  SELECT count(*) INTO n_new
  FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
  WHERE n.nspname = 'public'
    AND p.proname = 'maintainer_ingest_food'
    AND p.pronargs = 15;
  ASSERT n_new = 1, '036: new 15-param maintainer_ingest_food not found';

  -- ingest.confidence_min must be gone
  SELECT count(*) INTO n_cmin
  FROM public.system_config
  WHERE key = 'ingest.confidence_min';
  ASSERT n_cmin = 0, '036: ingest.confidence_min still in system_config';

  RAISE NOTICE '036 verify: old-fn=% (expect 0), new-fn=% (expect 1), conf_min-rows=% (expect 0)',
    n_old, n_new, n_cmin;
END $$;
