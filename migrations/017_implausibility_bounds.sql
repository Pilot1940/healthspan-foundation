-- =============================================================
-- 017_implausibility_bounds.sql  (ADDITIVE — ALTER + UPDATE + idempotent INSERT)
-- PROMPT 18 — the implausibility BOUND, a second line of defence distinct from
-- the clinical reference range.
--
-- TWO DIFFERENT RANGES on metric_definitions, do not conflate them:
--   * min_value / max_value         = CLINICAL REFERENCE range. A breach is REAL
--                                     DATA (high insulin, high android fat). It
--                                     WRITES to prod and is flagged abnormal
--                                     (rule #8 / abnormal_labs). NEVER gated.
--   * plausible_min / plausible_max = PHYSIOLOGICALLY-POSSIBLE extremes, MUCH
--                                     wider than clinical. A value OUTSIDE these
--                                     is almost certainly an OCR / unit / comma
--                                     error — it is GATED: blocked from prod and
--                                     routed to staging for human review.
--
-- Both new columns are NULLABLE. NULL plausible bound = "unknown" → lib/contract
-- falls back to a per-(profile, metric) heuristic (reject if >10x prior max or
-- <0.1x prior min), so unseeded metrics are still protected from gross errors.
--
-- Seeds are deliberately GENEROUS — a real but extreme value must still pass the
-- bound (and merely flag abnormal via the clinical range). When unsure, widen.
-- =============================================================
BEGIN;

-- ----------------------------------------------------------------
-- 1. Columns (nullable) + documentation
-- ----------------------------------------------------------------
ALTER TABLE public.metric_definitions
  ADD COLUMN IF NOT EXISTS plausible_min numeric,
  ADD COLUMN IF NOT EXISTS plausible_max numeric;

COMMENT ON COLUMN public.metric_definitions.plausible_min IS
  'Physiologically POSSIBLE lower extreme (NOT the clinical reference min_value). A value BELOW this is gated → staging (likely OCR/unit/comma error). NULL = unknown → contract uses the per-(profile,metric) 10x/0.1x heuristic. Much wider than min_value.';
COMMENT ON COLUMN public.metric_definitions.plausible_max IS
  'Physiologically POSSIBLE upper extreme (NOT the clinical reference max_value). A value ABOVE this is gated → staging (likely OCR/unit/comma error). NULL = unknown → contract uses the per-(profile,metric) 10x/0.1x heuristic. Much wider than max_value.';

-- ----------------------------------------------------------------
-- 2. Seed plausible bounds on EXISTING metrics (UPDATE by name).
--    Idempotent: a re-run just re-sets the same numbers.
--    Bounds are wide physiological extremes, not clinical ranges.
-- ----------------------------------------------------------------
UPDATE public.metric_definitions AS m
SET    plausible_min = v.pmin, plausible_max = v.pmax
FROM (VALUES
  -- hormones
  ('testosterone_total',     50.0,    2000.0),   -- ng/dL  (clinical ~300-1000)
  ('testosterone_free',      1.0,     500.0),     -- pg/mL
  -- metabolic
  ('fasting_glucose',        30.0,    800.0),     -- mg/dL  (30=severe hypo, 800=HHS)
  ('fasting_insulin',        0.2,     500.0),     -- µIU/mL
  ('hba1c',                  2.0,     20.0),      -- %
  -- lipids (mg/dL) — generous so a real high value flags but doesn't gate
  ('hdl',                    5.0,     200.0),
  ('ldl',                    5.0,     800.0),
  ('non_hdl',                5.0,     900.0),
  ('vldl',                   1.0,     500.0),
  ('triglycerides',          10.0,    10000.0),   -- severe hypertriglyceridaemia is real
  ('chol_hdl_ratio',         0.2,     50.0),      -- ratio
  ('hdl_ldl_ratio',          0.01,    20.0),      -- ratio
  -- vitamins
  ('vitamin_d',              1.0,     200.0),     -- ng/mL (toxicity territory >150)
  -- body composition
  ('body_weight_kg',         30.0,    300.0),     -- kg
  ('body_fat_pct',           2.0,     75.0),      -- %
  ('lean_mass_kg',           10.0,    150.0),     -- kg
  ('lean_mass_arms_kg',      0.5,     30.0),
  ('lean_mass_legs_kg',      2.0,     60.0),
  ('lean_mass_trunk_kg',     3.0,     80.0),
  ('android_fat_pct',        1.0,     80.0),      -- %
  ('gynoid_fat_pct',         1.0,     80.0),      -- %
  -- android_gynoid_ratio: clinical max is 1.0, but a ratio of 1.27 is a REAL
  -- (abnormal) reading — bound must be WIDE so it writes+flags, never gates.
  ('android_gynoid_ratio',   0.0,     5.0)        -- ratio
) AS v(name, pmin, pmax)
WHERE m.name = v.name;

-- ----------------------------------------------------------------
-- 3. Seed food / energy metrics that may not yet exist (idempotent INSERT).
--    food_energy_kcal is the bound lib/contract uses to gate food_logs.calories
--    — the root-cause backstop for the "1,020 kcal" -> 20 comma-slip. A logged
--    food entry under ~25 kcal is implausible enough to route to review (the
--    value is staged, never lost). energy_burned_cal documents the workout bound.
-- ----------------------------------------------------------------
INSERT INTO public.metric_definitions
  (name, display_name, category, data_type, unit, min_value, max_value,
   plausible_min, plausible_max, description, is_active)
SELECT v.name, v.display_name, v.category, 'numeric', v.unit,
       v.cmin, v.cmax, v.pmin, v.pmax, v.dsc, true
FROM (VALUES
  ('food_energy_kcal', 'Food Energy (logged)', 'nutrition', 'kcal',
      NULL::numeric, NULL::numeric, 25.0, 12000.0,
      'Energy of a logged food entry (kcal). plausible_min gates the "1,020"->20 comma-slip; a sub-25-kcal entry routes to staging for review.'),
  ('energy_burned_cal', 'Energy Burned', 'activity', 'kcal',
      NULL::numeric, NULL::numeric, 0.0, 10000.0,
      'Energy burned over a cycle/workout (kcal). 0 is valid (rest); >10000 is implausible for a single period.')
) AS v(name, display_name, category, unit, cmin, cmax, pmin, pmax, dsc)
WHERE NOT EXISTS (
    SELECT 1 FROM public.metric_definitions WHERE name = v.name
);

-- If those rows already existed (re-run), make sure their bounds are set too.
UPDATE public.metric_definitions
SET plausible_min = 25.0, plausible_max = 12000.0
WHERE name = 'food_energy_kcal' AND plausible_min IS NULL;
UPDATE public.metric_definitions
SET plausible_min = 0.0, plausible_max = 10000.0
WHERE name = 'energy_burned_cal' AND plausible_min IS NULL;

-- ----------------------------------------------------------------
-- 4. In-transaction verification (visible in dry-run, before COMMIT/ROLLBACK)
-- ----------------------------------------------------------------
DO $$
DECLARE
    n_bounded int;
    n_food    int;
    ag_max    numeric;
BEGIN
    SELECT count(*) INTO n_bounded FROM public.metric_definitions
      WHERE plausible_min IS NOT NULL OR plausible_max IS NOT NULL;
    SELECT count(*) INTO n_food FROM public.metric_definitions
      WHERE name = 'food_energy_kcal' AND plausible_min = 25.0;
    SELECT plausible_max INTO ag_max FROM public.metric_definitions
      WHERE name = 'android_gynoid_ratio';
    RAISE NOTICE '017: % metric_definitions now carry plausibility bounds', n_bounded;
    RAISE NOTICE '017: food_energy_kcal bounded at 25 kcal min: % (expect 1)', n_food;
    RAISE NOTICE '017: android_gynoid_ratio plausible_max = % (expect 5.0; clinical max stays 1.0)', ag_max;
END $$;

COMMIT;
