-- =============================================================
-- 013_bodycomp_canonical_biomarkers.sql  (DATA — fold + retire)
-- PROMPT 15. Make `biomarkers` the SINGLE home for DEXA body composition.
-- All 16 body_metrics_history rows are the 2026-06-02 11:00 Miracles DEXA; 9 already
-- have an exact biomarkers twin, 7 are missing. Fold the 7 in (idempotent on the 008a
-- key), preserve the clinical interpretation as ONE health_context_notes 'log' entry
-- (notes are narrative, not data → never written into biomarkers.value), then empty
-- body_metrics_history of DEXA data. The table is KEPT (dormant) for future frequent
-- scale/body-fat data — but DEXA must never write there again (canonical home = biomarkers).
-- =============================================================
BEGIN;

-- 1. Fold every body_metrics_history row into biomarkers (INSERT if missing).
--    Idempotent: ON CONFLICT (profile_id, metric_definition_id, measured_at) DO NOTHING.
INSERT INTO public.biomarkers
  (profile_id, metric_definition_id, value, unit, measured_at, source, lab_name, notes)
SELECT bmh.profile_id, bmh.metric_definition_id, bmh.value, bmh.unit, bmh.measured_at,
       bmh.source, 'Miracles Healthcare', 'DEXA body comp (folded from body_metrics_history, 013)'
FROM public.body_metrics_history bmh
ON CONFLICT (profile_id, metric_definition_id, measured_at) DO NOTHING;

-- 2. GATE: every body_metrics_history row must now have a biomarkers twin.
DO $$
DECLARE n_orphan integer;
BEGIN
  SELECT count(*) INTO n_orphan
  FROM public.body_metrics_history bmh
  WHERE NOT EXISTS (
    SELECT 1 FROM public.biomarkers b
    WHERE b.profile_id=bmh.profile_id
      AND b.metric_definition_id=bmh.metric_definition_id
      AND b.measured_at=bmh.measured_at);
  IF n_orphan > 0 THEN
    RAISE EXCEPTION '013 STOP: % body_metrics_history rows have no biomarkers twin.', n_orphan;
  END IF;
  RAISE NOTICE '013: all body_metrics_history rows now covered in biomarkers.';
END $$;

-- 3. Preserve the clinical interpretation as a health_context_notes 'log' entry.
INSERT INTO public.health_context_notes (profile_id, kind, summary, refs)
SELECT DISTINCT bmh.profile_id, 'log',
  'DEXA 2026-06-02 (Miracles): body fat 39.2 pct (target 20), android fat 53.8 pct + A/G 1.27 = '
  'central adiposity correlating with IR; lean 59.1 kg (~10-15 kg below optimal for 179cm); '
  'need to lose ~18 kg fat. BMI 31.5 (obese). Numbers in biomarkers @ 2026-06-02.',
  jsonb_build_object('biomarkers','2026-06-02 DEXA body composition')
FROM public.body_metrics_history bmh
WHERE bmh.source='dexa_scan'
LIMIT 1;

-- 4. Empty body_metrics_history of the DEXA data (canonical copy now in biomarkers).
--    Table kept (dormant) for future non-DEXA scale/body-fat data.
DELETE FROM public.body_metrics_history WHERE source='dexa_scan';

DO $$
DECLARE n integer;
BEGIN
  SELECT count(*) INTO n FROM public.body_metrics_history;
  RAISE NOTICE '013: body_metrics_history emptied of DEXA; % rows remain (dormant table kept).', n;
END $$;

COMMIT;
