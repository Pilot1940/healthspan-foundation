-- =============================================================
-- 019_widen_clinically_tight_bounds.sql  (ADDITIVE — UPDATE only)
-- Widen three implausibility ceilings/floors (from 018) that sat BELOW known
-- severe-pathology extremes, so a real critical value flows to prod (→ abnormal_labs
-- flagged HIGH/LOW) instead of being gated to staging.
--
-- Principle: the plausibility bound rejects only physically-impossible / unit-slip
-- values. It must sit ABOVE (or below) the worst real pathology, NOT at the edge of
-- it — otherwise a genuine critical result is treated like an OCR error. The clinical
-- reference range (min_value/max_value) is what flags abnormal; the plausibility
-- bound is the OCR/unit backstop only.
--
--   tsh         plausible_max 150 → 500   (severe untreated myxedema runs 200–400 µIU/mL)
--   vitamin_b12 plausible_min  30 →   5   (severe pernicious anaemia reads <30 pg/mL)
--   uric_acid   plausible_max  30 →  40   (tumour-lysis / severe gout ~20–25; headroom)
--
-- A 10–100x slip beyond the new bound is still caught (e.g. tsh 5000, uric_acid 600,
-- b12 4 → gated). A slip that lands WITHIN pathological-but-real range (e.g. a normal
-- tsh 2.5 mis-read as 250) now flows to abnormal_labs flagged HIGH — by design, it is
-- indistinguishable from a real severe value and a human reviews it there.
-- =============================================================
BEGIN;

UPDATE public.metric_definitions AS m
SET    plausible_min = v.pmin, plausible_max = v.pmax
FROM (VALUES
  ('tsh',          0.001,  500.0),
  ('vitamin_b12',  5.0,    200000.0),
  ('uric_acid',    0.1,    40.0)
) AS v(name, pmin, pmax)
WHERE m.name = v.name;

DO $$
DECLARE r record;
BEGIN
    FOR r IN SELECT name, plausible_min, plausible_max FROM public.metric_definitions
             WHERE name IN ('tsh','vitamin_b12','uric_acid') ORDER BY name
    LOOP
        RAISE NOTICE '019: % plausible bound now [%, %]', r.name, r.plausible_min, r.plausible_max;
    END LOOP;
END $$;

COMMIT;
