-- =============================================================
-- 008d_metric_definitions_catalog.sql  (ADDITIVE — INSERTs only)
-- Extend the metric_definitions catalog with markers missing from
-- the Feb 2026 Dr Lal panel and cardiac/respiratory point measures.
--
-- Design decisions baked in:
--   * ag_ratio already exists mapped to Android/Gynoid (body_composition).
--     Albumin:Globulin ratio is a DIFFERENT marker → new row 'albumin_globulin_ratio'
--   * Lp(a) '<10.00' is below-detection — the NUMERIC value is correct (10) but
--     the qualifier '<' is stored in notes on the biomarkers row, not here.
--   * Non-scalar findings (ECG narrative, Holter rhythm, stress echo report text)
--     are NOT forced into metric_definitions — they belong in the documents table.
--   * HOMA-IR: definition already exists (confirmed). Not re-added.
--   * Duplicate check: INSERT … WHERE NOT EXISTS on name — fully idempotent.
--
-- Categories added / extended:
--   blood_count    : CBC differentials (PCV, MCV, MCH, MCHC, RDW, MPV, Mentzer,
--                    5-part diff % + absolute counts)
--   lipids         : non_hdl, chol_hdl_ratio, apo_b_a1_ratio
--   metabolic      : eag (estimated avg glucose)
--   kidney         : bun
--   liver          : ggtp (already ggt → add alias 'ggtp'), globulin,
--                    albumin_globulin_ratio, ast_alt_ratio, bun_creatinine_ratio
--   minerals       : calcium, phosphorus, sodium, potassium, chloride
--   iron_studies   : tibc, transferrin_saturation
--   cardiac        : vo2max_estimated, vo2max_lab, systolic_bp, diastolic_bp,
--                    resting_hr (point measurement), lung_age, coronary_calcium_score
--   infectious     : hbsag_index, anti_hcv_index
--   pancreatic     : lipase, amylase
--   urine          : urine_specific_gravity, urine_ph, urine_acr, albumin_urine
-- =============================================================

-- Helper: only insert if name not already present
DO $$
BEGIN
    RAISE NOTICE '008d: extending metric_definitions catalog...';
END $$;

-- ----------------------------------------------------------------
-- BLOOD COUNT (CBC differentials)
-- ----------------------------------------------------------------
INSERT INTO public.metric_definitions (name, display_name, category, data_type, unit, min_value, max_value, description, is_active)
SELECT v.name, v.display_name, v.category, 'numeric', v.unit, v.lo, v.hi, v.dsc, true
FROM (VALUES
  ('pcv',                    'PCV (Haematocrit)',          'blood_count', '%',       40.0,  50.0,  'Packed cell volume — proportion of blood occupied by RBCs (males)'),
  ('mcv',                    'MCV',                        'blood_count', 'fL',      83.0, 101.0,  'Mean corpuscular volume — average size of RBCs'),
  ('mch',                    'MCH',                        'blood_count', 'pg',      27.0,  32.0,  'Mean corpuscular haemoglobin — average haemoglobin per RBC'),
  ('mchc',                   'MCHC',                       'blood_count', 'g/dL',    31.5,  34.5,  'Mean corpuscular haemoglobin concentration'),
  ('rdw',                    'RDW',                        'blood_count', '%',       11.6,  14.0,  'Red cell distribution width — variation in RBC size'),
  ('mpv',                    'MPV',                        'blood_count', 'fL',       6.5,  12.0,  'Mean platelet volume'),
  ('mentzer_index',          'Mentzer Index',              'blood_count', NULL,       NULL, 13.0,  'MCV/RBC; >13 suggests iron deficiency anaemia; <13 thalassaemia trait'),
  ('tlc',                    'TLC (WBC)',                  'blood_count', 'thou/mm3', 4.0,  10.0,  'Total leucocyte count — alias for wbc; use when sourced from Indian lab reports'),
  ('neutrophils_pct',        'Neutrophils %',              'blood_count', '%',       40.0,  80.0,  'Neutrophil differential percentage'),
  ('lymphocytes_pct',        'Lymphocytes %',              'blood_count', '%',       20.0,  40.0,  'Lymphocyte differential percentage'),
  ('monocytes_pct',          'Monocytes %',                'blood_count', '%',        2.0,  10.0,  'Monocyte differential percentage'),
  ('eosinophils_pct',        'Eosinophils %',              'blood_count', '%',        1.0,   6.0,  'Eosinophil differential percentage'),
  ('basophils_pct',          'Basophils %',                'blood_count', '%',        0.0,   2.0,  'Basophil differential percentage'),
  ('neutrophils_abs',        'Neutrophils Abs',            'blood_count', 'thou/mm3', 2.0,   7.0,  'Absolute neutrophil count'),
  ('lymphocytes_abs',        'Lymphocytes Abs',            'blood_count', 'thou/mm3', 1.0,   3.0,  'Absolute lymphocyte count'),
  ('monocytes_abs',          'Monocytes Abs',              'blood_count', 'thou/mm3', 0.2,   1.0,  'Absolute monocyte count'),
  ('eosinophils_abs',        'Eosinophils Abs',            'blood_count', 'thou/mm3', 0.0,   0.5,  'Absolute eosinophil count'),
  ('basophils_abs',          'Basophils Abs',              'blood_count', 'thou/mm3', 0.02,  0.1,  'Absolute basophil count')
) AS v(name, display_name, category, unit, lo, hi, dsc)
WHERE NOT EXISTS (
    SELECT 1 FROM public.metric_definitions WHERE name = v.name
);

-- ----------------------------------------------------------------
-- LIPIDS (derived ratios)
-- ----------------------------------------------------------------
INSERT INTO public.metric_definitions (name, display_name, category, data_type, unit, min_value, max_value, description, is_active)
VALUES
  ('non_hdl',        'Non-HDL Cholesterol',   'lipids', 'numeric', 'mg/dL', NULL,  130.0, 'Total Cholesterol minus HDL; includes all atherogenic particles', true),
  ('chol_hdl_ratio', 'Cholesterol:HDL Ratio', 'lipids', 'numeric', 'ratio', NULL,  4.4,   'Total cholesterol divided by HDL; <3.5 longevity optimal', true),
  ('apo_b_a1_ratio', 'ApoB/ApoA1 Ratio',      'lipids', 'numeric', 'ratio', NULL,  0.98,  'Apo B / Apo A1; <0.7 longevity optimal', true)
ON CONFLICT DO NOTHING;

-- ----------------------------------------------------------------
-- METABOLIC
-- ----------------------------------------------------------------
INSERT INTO public.metric_definitions (name, display_name, category, data_type, unit, min_value, max_value, description, is_active)
VALUES
  ('eag', 'eAG (Estimated Avg Glucose)', 'metabolic', 'numeric', 'mg/dL', NULL, 90.0, 'Estimated average glucose derived from HbA1c; longevity optimal <90', true)
ON CONFLICT DO NOTHING;

-- ----------------------------------------------------------------
-- KIDNEY
-- ----------------------------------------------------------------
INSERT INTO public.metric_definitions (name, display_name, category, data_type, unit, min_value, max_value, description, is_active)
VALUES
  ('bun',                 'BUN (Blood Urea Nitrogen)', 'kidney', 'numeric', 'mg/dL', 6.0,  20.0, 'Blood urea nitrogen — kidney filtration marker', true),
  ('bun_creatinine_ratio','BUN:Creatinine Ratio',      'kidney', 'numeric', 'ratio', 10.0, 20.0, 'BUN divided by creatinine; elevated in dehydration/pre-renal azotaemia', true)
ON CONFLICT DO NOTHING;

-- ----------------------------------------------------------------
-- LIVER
-- ----------------------------------------------------------------
INSERT INTO public.metric_definitions (name, display_name, category, data_type, unit, min_value, max_value, description, is_active)
VALUES
  ('ggtp',                   'GGT / GGTP',             'liver', 'numeric', 'U/L',   NULL, 73.0, 'Gamma-glutamyl transferase; alternate name for ggt', true),
  ('globulin',               'Globulin',               'liver', 'numeric', 'g/dL',  2.0,  3.5,  'Serum globulin (Total Protein minus Albumin)', true),
  ('albumin_globulin_ratio', 'Albumin:Globulin Ratio', 'liver', 'numeric', 'ratio', 0.9,  2.0,  'Albumin divided by globulin; NOT the same as ag_ratio (Android/Gynoid) in this DB', true),
  ('ast_alt_ratio',          'AST:ALT Ratio',          'liver', 'numeric', 'ratio', NULL, 1.0,  'SGOT:SGPT ratio; <1 typical for non-alcoholic; >2 suggests alcoholic hepatitis', true)
ON CONFLICT DO NOTHING;

-- ----------------------------------------------------------------
-- MINERALS / ELECTROLYTES
-- ----------------------------------------------------------------
INSERT INTO public.metric_definitions (name, display_name, category, data_type, unit, min_value, max_value, description, is_active)
VALUES
  ('calcium',   'Calcium (Serum)',    'minerals', 'numeric', 'mg/dL',  8.70,  10.40, 'Serum calcium — bone/cardiac/nerve signal marker', true),
  ('phosphorus','Phosphorus (Serum)', 'minerals', 'numeric', 'mg/dL',  2.40,   5.10, 'Serum phosphorus — bone mineralisation cofactor', true),
  ('sodium',    'Sodium (Serum)',     'minerals', 'numeric', 'mEq/L', 136.0, 145.0,  'Serum sodium — fluid balance electrolyte', true),
  ('potassium', 'Potassium (Serum)',  'minerals', 'numeric', 'mEq/L',  3.50,   5.10, 'Serum potassium — cardiac rhythm critical', true),
  ('chloride',  'Chloride (Serum)',   'minerals', 'numeric', 'mEq/L', 98.0,  107.0,  'Serum chloride — acid-base balance', true)
ON CONFLICT DO NOTHING;

-- ----------------------------------------------------------------
-- IRON STUDIES
-- ----------------------------------------------------------------
INSERT INTO public.metric_definitions (name, display_name, category, data_type, unit, min_value, max_value, description, is_active)
VALUES
  ('tibc',                  'TIBC',                  'minerals', 'numeric', 'µg/dL', 250.0, 425.0, 'Total iron binding capacity — marker of iron storage status', true),
  ('transferrin_saturation','Transferrin Saturation','minerals', 'numeric', '%',       20.0,  50.0, 'Iron divided by TIBC × 100; low in iron deficiency, high in overload', true)
ON CONFLICT DO NOTHING;

-- ----------------------------------------------------------------
-- PANCREATIC ENZYMES / URINE / INFECTIOUS / CARDIAC
-- (flat VALUES — avoids NULL type-inference issues in SELECT...FROM VALUES)
-- ----------------------------------------------------------------
INSERT INTO public.metric_definitions
  (name, display_name, category, data_type, unit, min_value, max_value, description, is_active)
VALUES
  -- pancreatic
  ('lipase',                'Lipase',                          'enzymes',     'numeric','U/L',             NULL,  67.0,  'Pancreatic lipase; elevated in pancreatitis',                                     true),
  ('amylase',               'Amylase',                         'enzymes',     'numeric','U/L',             28.0,  100.0, 'Pancreatic + salivary amylase',                                                   true),
  -- urine
  ('urine_specific_gravity','Urine Specific Gravity',          'kidney',      'numeric', NULL,            1.000, 1.030, 'Urine concentration; >1.030 = dehydration',                                       true),
  ('urine_ph',              'Urine pH',                        'kidney',      'numeric', NULL,            5.0,   8.0,   'Urine acidity; <5.5 with high protein/IF is common',                              true),
  ('albumin_urine',         'Albumin Urine',                   'kidney',      'numeric','mg/L',           NULL,  30.0,  'Urine albumin (not serum albumin); <30 = A1 (normal)',                             true),
  ('urine_acr',             'Urine ACR',                       'kidney',      'numeric','mg/g creatinine',NULL,  30.0,  'Albumin-creatinine ratio; <30 A1 normal category',                                true),
  -- infectious
  ('hbsag',                 'HBsAg (Hepatitis B Surface Ag)',  'inflammation','text',   NULL,            NULL,  NULL,  'Hepatitis B surface antigen; Non Reactive = negative',                            true),
  ('anti_hcv',              'Anti-HCV (Hepatitis C Ab)',       'inflammation','numeric','index',         NULL,  NULL,  'Anti-hepatitis C antibody index; <1.0 = non-reactive',                            true),
  -- cardiac / respiratory
  ('vo2max_estimated',      'VO2 Max (Estimated)',             'cardiac',     'numeric','ml/kg/min',     NULL,  NULL,  'VO2 max from wearable/algorithm estimate (e.g. WHOOP)',                            true),
  ('vo2max_lab',            'VO2 Max (Lab Measured)',          'cardiac',     'numeric','ml/kg/min',     NULL,  NULL,  'VO2 max from formal treadmill/ergometer test',                                    true),
  ('systolic_bp',           'Systolic Blood Pressure',         'cardiac',     'numeric','mmHg',          NULL,  130.0, 'Systolic blood pressure; <120 optimal',                                           true),
  ('diastolic_bp',          'Diastolic Blood Pressure',        'cardiac',     'numeric','mmHg',          NULL,  85.0,  'Diastolic blood pressure; <80 optimal',                                           true),
  ('resting_hr_spot',       'Resting HR (Spot Check)',         'cardiac',     'numeric','bpm',           NULL,  70.0,  'Resting HR from a single point measurement (vs WHOOP daily avg)',                 true),
  ('lung_age',              'Lung Age (Spirometry)',            'cardiac',     'numeric','years',         NULL,  NULL,  'Spirometry-derived lung age; PC baseline 53y at chronological age 42',            true),
  ('coronary_calcium_score','Coronary Calcium Score',          'cardiac',     'numeric','AU',            NULL,  NULL,  'Agatston score from CT; 0 = no detectable plaque',                                true)
ON CONFLICT DO NOTHING;

DO $$
DECLARE new_count integer;
BEGIN
    SELECT count(*) INTO new_count FROM public.metric_definitions;
    RAISE NOTICE '008d complete. metric_definitions now has % rows.', new_count;
END $$;
