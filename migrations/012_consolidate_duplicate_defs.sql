-- =============================================================
-- 012_consolidate_duplicate_defs.sql  (DATA + CATALOG cleanup)
-- Resolve two redundant/overloaded metric_definitions:
--
-- 1. ag_ratio was OVERLOADED — held BOTH albumin:globulin (lab LFT panels) AND
--    android/gynoid (DEXA). Split by source:
--      - lab rows (Dr Lal / Max Lab: 1.4, 1.5, 1.68) → albumin_globulin_ratio (liver)
--      - DEXA row  (Miracles: 1.27)                  → android_gynoid_ratio (body comp)
--    then drop ag_ratio (0 rows after move; no external FK references — verified).
--
-- 2. ggt vs ggtp — same marker (gamma-glutamyl transferase). Merge ggtp's 3 rows
--    into the canonical ggt, then drop ggtp.
--
-- No date collisions on any move (verified). No double-counting existed (no date
-- had both populated) — this is concept hygiene, not a data-correctness fix.
-- =============================================================
BEGIN;

-- ---- 1a. ag_ratio (lab) → albumin_globulin_ratio ----
UPDATE public.biomarkers
SET metric_definition_id = (SELECT id FROM metric_definitions WHERE name='albumin_globulin_ratio')
WHERE metric_definition_id = (SELECT id FROM metric_definitions WHERE name='ag_ratio')
  AND lab_name IN ('Dr Lal PathLabs','Max Lab');

-- ---- 1b. ag_ratio (DEXA) → android_gynoid_ratio ----
UPDATE public.biomarkers
SET metric_definition_id = (SELECT id FROM metric_definitions WHERE name='android_gynoid_ratio')
WHERE metric_definition_id = (SELECT id FROM metric_definitions WHERE name='ag_ratio');

-- ---- 1c. ag_ratio in body_metrics_history (DEXA) → android_gynoid_ratio ----
--    (separate table from biomarkers; the row is a dexa_scan android/gynoid value)
UPDATE public.body_metrics_history
SET metric_definition_id = (SELECT id FROM metric_definitions WHERE name='android_gynoid_ratio')
WHERE metric_definition_id = (SELECT id FROM metric_definitions WHERE name='ag_ratio');

-- ---- 2. ggtp → ggt (biomarkers + body_metrics_history) ----
UPDATE public.biomarkers
SET metric_definition_id = (SELECT id FROM metric_definitions WHERE name='ggt')
WHERE metric_definition_id = (SELECT id FROM metric_definitions WHERE name='ggtp');
UPDATE public.body_metrics_history
SET metric_definition_id = (SELECT id FROM metric_definitions WHERE name='ggt')
WHERE metric_definition_id = (SELECT id FROM metric_definitions WHERE name='ggtp');

-- ---- 3. drop the now-empty redundant definitions (guarded across BOTH tables) ----
DO $$
DECLARE n_ag integer; n_ggtp integer;
BEGIN
  SELECT (SELECT count(*) FROM biomarkers WHERE metric_definition_id=(SELECT id FROM metric_definitions WHERE name='ag_ratio'))
       + (SELECT count(*) FROM body_metrics_history WHERE metric_definition_id=(SELECT id FROM metric_definitions WHERE name='ag_ratio'))
    INTO n_ag;
  SELECT (SELECT count(*) FROM biomarkers WHERE metric_definition_id=(SELECT id FROM metric_definitions WHERE name='ggtp'))
       + (SELECT count(*) FROM body_metrics_history WHERE metric_definition_id=(SELECT id FROM metric_definitions WHERE name='ggtp'))
    INTO n_ggtp;
  IF n_ag = 0 THEN
    DELETE FROM metric_definitions WHERE name='ag_ratio';
    RAISE NOTICE '012: ag_ratio removed (rows re-pointed to albumin_globulin_ratio / android_gynoid_ratio).';
  ELSE
    RAISE EXCEPTION '012 STOP: ag_ratio still has % biomarker rows — investigate.', n_ag;
  END IF;
  IF n_ggtp = 0 THEN
    DELETE FROM metric_definitions WHERE name='ggtp';
    RAISE NOTICE '012: ggtp removed (rows merged into ggt).';
  ELSE
    RAISE EXCEPTION '012 STOP: ggtp still has % biomarker rows — investigate.', n_ggtp;
  END IF;
END $$;

COMMIT;
