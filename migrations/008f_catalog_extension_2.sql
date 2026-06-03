-- =============================================================
-- 008f_catalog_extension_2.sql  (ADDITIVE — INSERTs only)
-- Second metric_definitions extension, for markers surfaced by the
-- 2026-06-03 gap-import (Sep2024 Max Lab, Aug2025/Aug2023 Dr Lal, Nura 2023).
-- These recur across panels or are clinically useful; the gap-import inserts
-- their values after this lands.
--
-- android_gynoid_ratio is DISTINCT from ag_ratio. In this DB ag_ratio currently
-- holds BOTH albumin:globulin (lab panels) and android/gynoid (DEXA) historically
-- — going forward DEXA A/G uses android_gynoid_ratio, lab A:G uses albumin_globulin_ratio.
--
-- Idempotent: ON CONFLICT (name) DO NOTHING.
-- =============================================================
INSERT INTO public.metric_definitions
  (name, display_name, category, data_type, unit, min_value, max_value, description, is_active)
VALUES
  ('bilirubin_direct',   'Bilirubin (Direct)',        'liver',            'numeric','mg/dL',  NULL, 0.3,  'Conjugated bilirubin', true),
  ('bilirubin_indirect', 'Bilirubin (Indirect)',      'liver',            'numeric','mg/dL',  NULL, 1.0,  'Unconjugated bilirubin', true),
  ('bicarbonate',        'Bicarbonate (HCO3)',        'minerals',         'numeric','mmol/L', 22.0, 29.0, 'Serum bicarbonate — acid-base balance', true),
  ('pth_intact',         'Intact Parathyroid Hormone','hormones',         'numeric','pg/mL',  15.0, 65.0, 'iPTH — calcium/bone regulation', true),
  ('uibc',               'UIBC',                      'minerals',         'numeric','µg/dL',  NULL, NULL, 'Unsaturated iron binding capacity', true),
  ('ldh',                'LDH',                       'enzymes',          'numeric','IU/L',   NULL, NULL, 'Lactate dehydrogenase — tissue turnover', true),
  ('g6pd',               'G-6-PD',                    'enzymes',          'numeric','U/g Hb', NULL, NULL, 'Glucose-6-phosphate dehydrogenase', true),
  ('anti_hbs_titre',     'Anti-HBs Titre',            'inflammation',     'numeric','mIU/mL', 10.0, NULL, 'Hepatitis B surface antibody titre (>10 = immune)', true),
  ('creatinine_urine',   'Creatinine, Urine',         'kidney',           'numeric','mg/dL',  NULL, NULL, 'Urine creatinine (for ACR denominator)', true),
  ('hdl_ldl_ratio',      'HDL/LDL Ratio',             'lipids',           'numeric','ratio',  NULL, NULL, 'HDL divided by LDL', true),
  ('visceral_fat_area_ct','Visceral Adipose Tissue Area','body_composition','numeric','cm²', NULL, 130.0,'CT/DEXA visceral fat area; <130 cm² lower risk', true),
  ('visceral_fat_mass',  'Visceral Adipose Tissue Mass','body_composition','numeric','g',    NULL, NULL, 'Visceral fat mass', true),
  ('subcutaneous_fat_area','Subcutaneous Adipose Tissue Area','body_composition','numeric','cm²',NULL,NULL,'Subcutaneous fat area', true),
  ('android_gynoid_ratio','Android/Gynoid Fat Ratio', 'body_composition', 'numeric','ratio',  NULL, 1.0,  'DEXA android%/gynoid% fat ratio; distinct from ag_ratio (albumin:globulin)', true)
ON CONFLICT (name) DO NOTHING;

DO $$
DECLARE n integer;
BEGIN
    SELECT count(*) INTO n FROM public.metric_definitions;
    RAISE NOTICE '008f complete. metric_definitions now has % rows.', n;
END $$;
