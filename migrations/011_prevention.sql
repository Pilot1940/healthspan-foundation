-- =============================================================
-- 011_prevention.sql  (ADDITIVE — prevention rule #4)
-- PROMPT 11. Add a below-detection / above-range qualifier to biomarkers so
-- "<10", ">1000" etc. are preserved WITH the value instead of silently dropped.
-- Backfill lpa (Lp(a) "<10" was stored as 10 with the qualifier only in notes).
--
-- The other prevention rules (#1 number parsing, #2 content dedup, #3 never-drop
-- + reconcile, #5 dates-from-source) are enforced in CODE — lib/contract.py +
-- tests — not schema. #6 catalog extension already done (008d/008f).
-- =============================================================
BEGIN;

ALTER TABLE public.biomarkers
  ADD COLUMN IF NOT EXISTS qualifier text
  CHECK (qualifier IS NULL OR qualifier IN ('<','>','<=','>=','~'));

COMMENT ON COLUMN public.biomarkers.qualifier IS
  'Below/above-detection operator preserved with value: ''<'' means value is an upper bound (e.g. <10), ''>'' a lower bound. NULL = exact measurement.';

-- Backfill lpa: the 2026-02-19 Lp(a) was reported "<10.00" (below detection).
UPDATE public.biomarkers
SET qualifier='<'
WHERE metric_definition_id = (SELECT id FROM metric_definitions WHERE name='lpa')
  AND value = 10
  AND qualifier IS NULL;

DO $$
DECLARE n integer;
BEGIN
  GET DIAGNOSTICS n = ROW_COUNT;
  RAISE NOTICE '011: qualifier column added; lpa backfilled (% row(s)).', n;
END $$;

COMMIT;
