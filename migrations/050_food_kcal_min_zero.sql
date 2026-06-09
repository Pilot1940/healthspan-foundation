-- =============================================================
-- 050_food_kcal_min_zero.sql
-- Lower the food calorie plausibility floor from 25 → 0 kcal.
-- A black coffee (~2 kcal), tea, water, broth, or electrolyte drink is a legitimate
-- low/zero-calorie log; the 25 floor false-flagged them ("implausible calories: 2 kcal
-- (must be 25–12000)") and pushed them to clarify. The completeness + confidence gates
-- still catch genuinely-missing data (null calories) and uncertain identity.
-- Threshold lives in metric_definitions (a table), per the no-hardcoded-thresholds rule.
-- =============================================================
BEGIN;
UPDATE public.metric_definitions
   SET plausible_min = 0
 WHERE name = 'food_energy_kcal';
COMMIT;
