-- =============================================================
-- 004_supplements_nutrition.sql
-- Extends the foundation (001) + multi-tenant layer (002). Reconciliation, not greenfield.
-- Postgres 17.6 / Supabase project dsnydskkjwziynwmzfkh. Multi-tenant (post-002).
-- Idempotent (IF NOT EXISTS / DROP POLICY IF EXISTS / ON CONFLICT). BEGIN/COMMIT.
--
-- DELIVERS:
--   (A) Supplement/medication REGIMEN + INTAKE + CORRELATION-ready layer.
--   (B) A SHARED, family-wide FOOD-GUIDANCE list (avoid/minimize/superfood), de-branded.
--
-- KEY DECISIONS (justified inline):
--   * NEW per-person tables key purely on profile_id (NOT NULL). 002 kept user_id on
--     PRE-EXISTING tables only for rollback; new tables don't carry that debt.
--   * supplements catalog MIRRORS metric_definitions (shared reference + alias child),
--     BUT allows user-owned custom rows (owner_profile_id) — see policy justification.
--   * food_guidance is GLOBAL (no profile_id) like loinc_reference/metric_definitions.
--   * EXPOSURE is DATE-WINDOW based (D BETWEEN start_date AND COALESCE(end_date,today)),
--     NOT status='active' — discontinued/historical regimens MUST appear in correlations.
--   * Compound products (e.g. Viome box) resolved to ingredients via supplement_components
--     so "Ashwagandha ON vs OFF" works even when Ashwagandha is also inside a box.
--
-- PRE-RUN NOTE: pgcrypto lives in schema `extensions` on this project (per 002).
--   No new pgcrypto calls here, but gen_random_uuid() is core PG — fine.
-- =============================================================
BEGIN;

-- =============================================================
-- A. SUPPLEMENT / MEDICATION CATALOG  (mirror of metric_definitions)
--    Shared reference list. Canonical rows: owner_profile_id IS NULL (service_role
--    seeds these). Custom rows: owner_profile_id set to a profile the caller can
--    access (mirrors metric_definitions' "shared catalog" pattern but with an
--    escape hatch for per-tenant additions, justified by the "allow custom" ask).
-- =============================================================
CREATE TABLE IF NOT EXISTS supplements (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name              TEXT NOT NULL UNIQUE,                 -- canonical key, e.g. 'berberine'
  display_name      TEXT NOT NULL,
  brand             TEXT,                                 -- e.g. 'KSM-66', 'Viome', NULL=generic
  key_active        TEXT,                                 -- active compound, e.g. 'EPA+DHA'
  category          TEXT NOT NULL DEFAULT 'supplement'
                      CHECK (category IN ('supplement','medication','probiotic','other')),
  default_unit      TEXT,                                 -- mg | mcg | IU | g | caps | billion_cfu
  is_medication     BOOLEAN NOT NULL DEFAULT false,       -- prescription/Rx vs OTC supplement
  is_compound       BOOLEAN NOT NULL DEFAULT false,       -- true => has supplement_components rows
  owner_profile_id  UUID REFERENCES profiles(id) ON DELETE CASCADE,  -- NULL = canonical/shared
  is_custom         BOOLEAN NOT NULL DEFAULT false,
  notes             TEXT,
  is_active         BOOLEAN NOT NULL DEFAULT true,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
ALTER TABLE supplements ENABLE ROW LEVEL SECURITY;

DROP TRIGGER IF EXISTS trg_supplements_updated_at ON supplements;
CREATE TRIGGER trg_supplements_updated_at BEFORE UPDATE ON supplements
  FOR EACH ROW EXECUTE FUNCTION update_updated_at();

-- Read: every authenticated user sees the whole catalog (canonical + custom).
-- (Custom names are de-identified compounds, not health data; matching the
--  metric_definitions precedent of a fully readable shared catalog.)
DROP POLICY IF EXISTS supplements_read ON supplements;
CREATE POLICY supplements_read ON supplements
  FOR SELECT TO authenticated USING (true);

-- Write: authenticated users may ONLY add/edit CUSTOM rows they own. Canonical
-- rows (owner_profile_id IS NULL) are writable by service_role only — so the
-- shared catalog can't be vandalised, but a family can add a niche supplement.
DROP POLICY IF EXISTS supplements_owner_write ON supplements;
CREATE POLICY supplements_owner_write ON supplements
  FOR INSERT TO authenticated
  WITH CHECK (owner_profile_id IS NOT NULL AND is_custom = true
              AND has_profile_access(owner_profile_id));
DROP POLICY IF EXISTS supplements_owner_update ON supplements;
CREATE POLICY supplements_owner_update ON supplements
  FOR UPDATE TO authenticated
  USING (owner_profile_id IS NOT NULL AND has_profile_access(owner_profile_id))
  WITH CHECK (owner_profile_id IS NOT NULL AND has_profile_access(owner_profile_id));

CREATE INDEX IF NOT EXISTS idx_supplements_category ON supplements(category) WHERE is_active = true;
CREATE INDEX IF NOT EXISTS idx_supplements_owner ON supplements(owner_profile_id) WHERE owner_profile_id IS NOT NULL;

-- -------------------------------------------------------------
-- A2. supplement_aliases  (mirror of canonical_aliases)
--     Lets the skill / AI extractor resolve free-text names
--     ('Vitamin D3', 'cholecalciferol', 'D-3 5000') -> one supplement_id.
-- -------------------------------------------------------------
CREATE TABLE IF NOT EXISTS supplement_aliases (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  alias         TEXT NOT NULL,
  supplement_id UUID NOT NULL REFERENCES supplements(id) ON DELETE CASCADE,
  source        TEXT,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (alias, source)
);
ALTER TABLE supplement_aliases ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS supplement_aliases_read ON supplement_aliases;
CREATE POLICY supplement_aliases_read ON supplement_aliases
  FOR SELECT TO authenticated USING (true);
CREATE INDEX IF NOT EXISTS idx_supplement_aliases_supp ON supplement_aliases(supplement_id);

-- -------------------------------------------------------------
-- A3. supplement_components  (compound product -> ingredient breakdown)
--     Resolves the Viome-box problem: the box contains Ashwagandha 95mg,
--     Zinc 4mg, Resveratrol 107mg, etc. Ingredient-level correlation
--     ("Ashwagandha ON vs OFF") joins through this table so box-Ashwagandha
--     and standalone-Ashwagandha both count as Ashwagandha exposure.
-- -------------------------------------------------------------
CREATE TABLE IF NOT EXISTS supplement_components (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  product_id      UUID NOT NULL REFERENCES supplements(id) ON DELETE CASCADE,  -- the box/blend
  component_id    UUID NOT NULL REFERENCES supplements(id) ON DELETE CASCADE,  -- the active ingredient
  amount          NUMERIC,                                                     -- per full daily dose of product
  unit            TEXT,
  notes           TEXT,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (product_id, component_id),
  CHECK (product_id <> component_id)
);
ALTER TABLE supplement_components ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS supplement_components_read ON supplement_components;
CREATE POLICY supplement_components_read ON supplement_components
  FOR SELECT TO authenticated USING (true);
CREATE INDEX IF NOT EXISTS idx_supplement_components_product   ON supplement_components(product_id);
CREATE INDEX IF NOT EXISTS idx_supplement_components_component ON supplement_components(component_id);

-- =============================================================
-- B. REGIMENS  (the core "how long I take things")
--    ONE row per continuous (profile, supplement, period). A paused-then-resumed
--    gap = TWO rows (the UNIQUE key on start_date supports it). Split doses
--    (Berberine lunch+dinner) are ONE row; the multiple times live in timing[]
--    (and/or per-dose intake_logs) — never split the period into two rows.
--
--    Cyclical (Ashwagandha 8-12wk on / 4wk off): cycle_days_on / cycle_days_off.
--    NULL on both = continuous. The exposure view applies the modular on/off math.
-- =============================================================
CREATE TABLE IF NOT EXISTS supplement_regimens (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  profile_id      UUID NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
  supplement_id   UUID NOT NULL REFERENCES supplements(id),
  dose_amount     NUMERIC,                              -- per single dose (one timing slot)
  dose_unit       TEXT,
  timing          TEXT[] NOT NULL DEFAULT ARRAY['anytime']::TEXT[],  -- subset of morning|lunch|dinner|bedtime|anytime
  frequency       TEXT NOT NULL DEFAULT 'daily'
                    CHECK (frequency IN ('daily','2x_daily','3x_daily','weekly','cyclical','as_needed')),
  cycle_days_on   INT,                                  -- cyclical only (e.g. 70 = ~10wk on)
  cycle_days_off  INT,                                  -- cyclical only (e.g. 28 = 4wk off)
  start_date      DATE,                                 -- NULL allowed ONLY when status='planned'
  end_date        DATE,                                 -- NULL = ongoing (duration derivable vs CURRENT_DATE)
  status          TEXT NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active','tapering','paused','discontinued','planned')),
  purpose         TEXT,                                 -- e.g. 'insulin resistance', 'recovery'
  sprint_id       UUID REFERENCES sprints(id) ON DELETE SET NULL,
  program_id      UUID REFERENCES training_programs(id) ON DELETE SET NULL,
  notes           TEXT,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  -- a non-planned regimen MUST have a start_date (you can't be "taking" something with no start)
  CONSTRAINT regimen_started CHECK (status = 'planned' OR start_date IS NOT NULL),
  -- cyclical requires both on/off windows
  CONSTRAINT regimen_cycle CHECK (
    frequency <> 'cyclical' OR (cycle_days_on IS NOT NULL AND cycle_days_off IS NOT NULL)),
  CONSTRAINT regimen_window CHECK (end_date IS NULL OR start_date IS NULL OR end_date >= start_date)
);
ALTER TABLE supplement_regimens ENABLE ROW LEVEL SECURITY;

DROP TRIGGER IF EXISTS trg_supplement_regimens_updated_at ON supplement_regimens;
CREATE TRIGGER trg_supplement_regimens_updated_at BEFORE UPDATE ON supplement_regimens
  FOR EACH ROW EXECUTE FUNCTION update_updated_at();

DROP POLICY IF EXISTS supplement_regimens_profile_access ON supplement_regimens;
CREATE POLICY supplement_regimens_profile_access ON supplement_regimens
  FOR ALL TO authenticated
  USING (has_profile_access(profile_id))
  WITH CHECK (has_profile_access(profile_id));

-- One continuous period per (profile, supplement, start_date). COALESCE makes
-- planned (NULL start) rows idempotent on the epoch sentinel.
CREATE UNIQUE INDEX IF NOT EXISTS uq_supplement_regimens_period
  ON supplement_regimens (profile_id, supplement_id,
                          COALESCE(start_date, '0001-01-01'::date));
CREATE INDEX IF NOT EXISTS idx_supplement_regimens_profile ON supplement_regimens(profile_id, status);
CREATE INDEX IF NOT EXISTS idx_supplement_regimens_window  ON supplement_regimens(profile_id, start_date, end_date);
CREATE INDEX IF NOT EXISTS idx_supplement_regimens_supp    ON supplement_regimens(supplement_id);

-- =============================================================
-- C. INTAKE LOGS  (per-dose adherence -> correlation)
--    Optional. The regimen window gives the *prescription*; intake_logs give
--    actual *adherence*. Sources: manual (PC types it), journal (backfilled from
--    whoop_journal took_* booleans), skill (Telegram/AI confirmed), csv (bulk).
--    taken_on (generated) + UNIQUE(profile,supplement,taken_on,source) = the
--    double-count guard: re-running the journal backfill is idempotent, and
--    manual+journal for the same day/supplement coexist (different source) so
--    the view can dedupe by source priority.
-- =============================================================
CREATE TABLE IF NOT EXISTS supplement_intake_logs (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  profile_id      UUID NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
  regimen_id      UUID REFERENCES supplement_regimens(id) ON DELETE SET NULL,
  supplement_id   UUID NOT NULL REFERENCES supplements(id),
  taken_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  taken_on        DATE GENERATED ALWAYS AS ((taken_at AT TIME ZONE 'UTC')::date) STORED,
  dose_amount     NUMERIC,
  dose_unit       TEXT,
  source          TEXT NOT NULL DEFAULT 'manual'
                    CHECK (source IN ('manual','journal','skill','csv')),
  notes           TEXT,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (profile_id, supplement_id, taken_on, source)
);
ALTER TABLE supplement_intake_logs ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS supplement_intake_logs_profile_access ON supplement_intake_logs;
CREATE POLICY supplement_intake_logs_profile_access ON supplement_intake_logs
  FOR ALL TO authenticated
  USING (has_profile_access(profile_id))
  WITH CHECK (has_profile_access(profile_id));
CREATE INDEX IF NOT EXISTS idx_supplement_intake_profile_date ON supplement_intake_logs(profile_id, taken_on DESC);
CREATE INDEX IF NOT EXISTS idx_supplement_intake_supp         ON supplement_intake_logs(supplement_id, taken_on DESC);
CREATE INDEX IF NOT EXISTS idx_supplement_intake_regimen      ON supplement_intake_logs(regimen_id);

-- -------------------------------------------------------------
-- C2. AI-extraction staging (Foundation rule #2).
--     source='skill' may originate from AI (Telegram free-text / photo). Raw
--     AI extraction MUST stage first; PC promotes to supplement_intake_logs.
--     manual/csv are user-confirmed and may write intake directly; the skill
--     path routes through here. (Mirrors stg_food_log_review from 002.)
-- -------------------------------------------------------------
CREATE TABLE IF NOT EXISTS stg_supplement_intake_review (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  profile_id      UUID NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
  document_id     UUID REFERENCES documents(id) ON DELETE SET NULL,
  sync_log_id     UUID REFERENCES wearable_sync_log(id) ON DELETE SET NULL,
  supplement_id   UUID REFERENCES supplements(id),
  extracted_name  TEXT NOT NULL,
  extracted_dose  NUMERIC,
  extracted_unit  TEXT,
  taken_at        TIMESTAMPTZ,
  confidence      NUMERIC CHECK (confidence >= 0 AND confidence <= 1),
  status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','approved','rejected','merged')),
  reviewed_at     TIMESTAMPTZ,
  raw_text        TEXT,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
ALTER TABLE stg_supplement_intake_review ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS stg_supplement_intake_review_access ON stg_supplement_intake_review;
CREATE POLICY stg_supplement_intake_review_access ON stg_supplement_intake_review
  FOR ALL TO authenticated
  USING (has_profile_access(profile_id))
  WITH CHECK (has_profile_access(profile_id));
CREATE INDEX IF NOT EXISTS idx_stg_supp_intake_profile ON stg_supplement_intake_review(profile_id, status);

-- =============================================================
-- D. SHARED FOOD-GUIDANCE LIST  (family-wide default, de-branded)
--    GLOBAL like loinc_reference/metric_definitions: NO profile_id. One row per
--    (item, classification). Read-all to authenticated; writes service_role only
--    (justified: this is curated family-default reference data, not per-person
--    health data — same trust model as the LOINC / metric catalogs in 001).
--    Per-person OVERRIDES live in food_rules (001) and take precedence — see view G3.
-- =============================================================
CREATE TABLE IF NOT EXISTS food_guidance (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  item            TEXT NOT NULL,
  classification  TEXT NOT NULL CHECK (classification IN ('avoid','minimize','superfood')),
  reason          TEXT,
  alternative     TEXT,                                 -- e.g. 'olive oil' for ghee
  category        TEXT,                                 -- optional grouping: fat | dairy | fruit | meat
  scope           TEXT NOT NULL DEFAULT 'family',       -- 'family' default; future per-cohort scoping
  source          TEXT NOT NULL DEFAULT 'curated',      -- de-branded origin (was a gut-test panel)
  is_active       BOOLEAN NOT NULL DEFAULT true,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (item, classification, scope)
);
ALTER TABLE food_guidance ENABLE ROW LEVEL SECURITY;
DROP TRIGGER IF EXISTS trg_food_guidance_updated_at ON food_guidance;
CREATE TRIGGER trg_food_guidance_updated_at BEFORE UPDATE ON food_guidance
  FOR EACH ROW EXECUTE FUNCTION update_updated_at();
-- Read-all; NO authenticated write policy => writes restricted to service_role.
DROP POLICY IF EXISTS food_guidance_read ON food_guidance;
CREATE POLICY food_guidance_read ON food_guidance
  FOR SELECT TO authenticated USING (true);
CREATE INDEX IF NOT EXISTS idx_food_guidance_class ON food_guidance(classification) WHERE is_active = true;

-- =============================================================
-- E. SYSTEM CONFIG  (Foundation rule #1 — no hardcoded thresholds/mappings)
--    Journal->supplement mapping + cyclical defaults live here, NOT in view bodies.
-- =============================================================
INSERT INTO system_config (key, value, description, category) VALUES
 ('supplements.journal_intake_map',
  '{"took_fish_oil":"omega_3","took_calcium_supplement":null,"took_electrolytes":null}'::jsonb,
  'Maps whoop_journal took_* booleans to supplements.name for the source=journal intake backfill. null = no real plan item (took_calcium_supplement -> only a DISCONTINUED Himalayan combo; took_electrolytes -> no exact plan item). Unmapped flags are NOT backfilled, never invented.',
  'supplements'),
 ('supplements.cyclical_default_days_on', '70'::jsonb,
  'Nominal ON window (days) for cyclical regimens where PC specified a range (e.g. Ashwagandha 8-12wk). ~10 weeks.',
  'supplements'),
 ('supplements.cyclical_default_days_off', '28'::jsonb,
  'Nominal OFF window (days) for cyclical regimens (e.g. Ashwagandha 4wk off).',
  'supplements'),
 ('supplements.intake_source_priority',
  '["manual","skill","csv","journal"]'::jsonb,
  'Dedupe priority when multiple intake rows exist for the same (profile,supplement,day). Highest-trust first; the exposure/adherence views collapse to the top-priority source.',
  'supplements')
ON CONFLICT (key) DO NOTHING;

-- =============================================================
-- F. SEED — supplements catalog (canonical: owner_profile_id NULL) + aliases
--    + compound components for the Viome box.
-- =============================================================
INSERT INTO supplements (name, display_name, brand, key_active, category, default_unit, is_medication, is_compound, notes) VALUES
 ('metformin',          'Metformin',                    NULL,      'metformin HCl',          'medication','mg',          true,  false, 'Rx; discontinued, replaced by berberine'),
 ('berberine',          'Berberine',                    NULL,      'berberine HCl',          'supplement','mg',          false, false, 'IR intervention; replaced metformin'),
 ('vitamin_d3',         'Vitamin D3',                   NULL,      'cholecalciferol',        'supplement','IU',          false, false, 'Take with a fat-containing meal'),
 ('vitamin_k2_mk7',     'Vitamin K2 (MK-7)',            NULL,      'menaquinone-7',          'supplement','mcg',         false, false, 'Co-dose with D3'),
 ('methylcobalamin_b12','Methylcobalamin (B12)',        NULL,      'methylcobalamin',        'supplement','mcg',         false, false, NULL),
 ('magnesium_bisglycinate','Magnesium Bisglycinate',    NULL,      'magnesium bisglycinate', 'supplement','mg',          false, false, 'Bedtime; calming form'),
 ('magnesium_citrate',  'Magnesium Citrate',            NULL,      'magnesium citrate',      'supplement','mg',          false, false, 'AM'),
 ('omega_3',            'Omega-3 (EPA+DHA)',            NULL,      'EPA+DHA',                'supplement','g',           false, false, 'Target >=2g/day EPA+DHA'),
 ('creatine',           'Creatine Monohydrate',         NULL,      'creatine monohydrate',   'supplement','g',           false, false, NULL),
 ('nac',                'NAC',                          NULL,      'N-acetylcysteine',       'supplement','mg',          false, false, NULL),
 ('nutritional_yeast',  'Nutritional Yeast',            NULL,      'B-vitamin yeast',        'other',     'g',           false, false, 'Food topping'),
 ('ashwagandha_ksm66',  'Ashwagandha (KSM-66)',         'KSM-66',  'withanolides',           'supplement','mg',          false, false, 'Cyclical on/off'),
 ('zinc_picolinate',    'Zinc Picolinate',              NULL,      'zinc picolinate',        'supplement','mg',          false, false, NULL),
 ('collagen',           'Collagen Peptides',            NULL,      'hydrolysed collagen',    'supplement','g',           false, false, 'Pre-training; planned'),
 ('vitamin_c',          'Vitamin C',                    NULL,      'ascorbic acid',          'supplement','mg',          false, false, 'Pre-training; planned'),
 ('nmn',                'NMN',                          NULL,      'nicotinamide mononucleotide','supplement','mg',       false, false, 'Planned; low priority'),
 ('resveratrol',        'Resveratrol',                  NULL,      'trans-resveratrol',      'supplement','mg',          false, false, 'Standalone planned; also a box component'),
 ('viome_precision_supplements','Precision Supplements (box)','Viome','multi-ingredient blend','supplement','caps',     false, true,  'Compound: 8 caps/day; see supplement_components'),
 ('viome_gut_formula',  'Gut Formula (probiotic)',      'Viome',   'L. plantarum Lp-115',    'probiotic', 'billion_cfu', false, false, '12.9B CFU'),
 ('viome_oral_lozenges','Oral Lozenges',                'Viome',   'oral microbiome blend',  'supplement','lozenge',     false, false, NULL),
 -- box-only sub-actives that need their own catalog id for component resolution
 ('vitamin_b6_p5p',     'Vitamin B6 (P5P)',             NULL,      'pyridoxal-5-phosphate',  'supplement','mg',          false, false, 'Viome box component'),
 ('vitamin_b9_5mthf',   'Folate (5-MTHF)',              NULL,      'L-methylfolate',         'supplement','mcg',         false, false, 'Viome box component')
ON CONFLICT (name) DO NOTHING;

-- Aliases (skill/AI name resolution)
INSERT INTO supplement_aliases (alias, supplement_id, source)
SELECT a.alias, s.id, 'seed'
FROM (VALUES
  ('Vitamin D-3','vitamin_d3'), ('cholecalciferol','vitamin_d3'), ('D3','vitamin_d3'),
  ('K2','vitamin_k2_mk7'), ('MK-7','vitamin_k2_mk7'), ('menaquinone','vitamin_k2_mk7'),
  ('B12','methylcobalamin_b12'), ('vitamin b12','methylcobalamin_b12'),
  ('Mag Bisglycinate','magnesium_bisglycinate'), ('magnesium glycinate','magnesium_bisglycinate'),
  ('Mag Citrate','magnesium_citrate'),
  ('fish oil','omega_3'), ('EPA','omega_3'), ('DHA','omega_3'), ('omega3','omega_3'),
  ('N-acetylcysteine','nac'), ('n acetyl cysteine','nac'),
  ('ashwagandha','ashwagandha_ksm66'), ('KSM 66','ashwagandha_ksm66'),
  ('zinc','zinc_picolinate'),
  ('vitamin c','vitamin_c'), ('ascorbic acid','vitamin_c'),
  ('trans-resveratrol','resveratrol'),
  ('precision supplements','viome_precision_supplements'),
  ('gut formula','viome_gut_formula')
) AS a(alias, name)
JOIN supplements s ON s.name = a.name
ON CONFLICT (alias, source) DO NOTHING;

-- Viome Precision Supplements box -> ingredient breakdown (per full 8-cap daily dose)
INSERT INTO supplement_components (product_id, component_id, amount, unit, notes)
SELECT p.id, c.id, v.amount, v.unit, v.notes
FROM (VALUES
  ('vitamin_d3',        1200::numeric, 'IU',  'box-included D3'),
  ('vitamin_b6_p5p',    19::numeric,   'mg',  'P5P'),
  ('vitamin_b9_5mthf',  544::numeric,  'mcg', '5-MTHF'),
  ('vitamin_k2_mk7',    NULL::numeric, 'mcg', 'box-included K2 (amount unspecified)'),
  ('ashwagandha_ksm66', 95::numeric,   'mg',  'box-included ashwagandha'),
  ('zinc_picolinate',   4::numeric,    'mg',  'box-included zinc'),
  ('resveratrol',       107::numeric,  'mg',  'box-included resveratrol')
) AS v(name, amount, unit, notes)
JOIN supplements c ON c.name = v.name
CROSS JOIN supplements p
WHERE p.name = 'viome_precision_supplements'
ON CONFLICT (product_id, component_id) DO NOTHING;

-- =============================================================
-- G. SEED — PC's regimens + food_guidance + journal-backfill intake.
--    Resolve PC's 'self' profile dynamically (same constant as 002).
--    Today = 2026-06-02. Literal dates here; CURRENT_DATE only in views.
-- =============================================================
DO $$
DECLARE
  v_pc CONSTANT UUID := '0b0e4093-6758-46f7-a6e2-311ef6828a86';
  v_pc_profile UUID;
  v_on  INT;
  v_off INT;
BEGIN
  SELECT id INTO v_pc_profile FROM profiles WHERE auth_user_id = v_pc;
  IF v_pc_profile IS NULL THEN
    RAISE NOTICE 'PC profile not found (auth_user_id=%); skipping 004 seeds.', v_pc;
    RETURN;
  END IF;

  SELECT (value #>> '{}')::int INTO v_on  FROM system_config WHERE key='supplements.cyclical_default_days_on';
  SELECT (value #>> '{}')::int INTO v_off FROM system_config WHERE key='supplements.cyclical_default_days_off';

  -- ---- REGIMENS (one INSERT ... SELECT join to resolve supplement_id by name) ----
  -- MEDS
  INSERT INTO supplement_regimens
    (profile_id, supplement_id, dose_amount, dose_unit, timing, frequency,
     cycle_days_on, cycle_days_off, start_date, end_date, status, purpose, notes)
  SELECT v_pc_profile, s.id, r.dose, r.unit, r.timing, r.freq,
         -- cyclical rows get the nominal on/off from system_config (v_on/v_off);
         -- the regimen_cycle CHECK requires both NON-NULL at INSERT time.
         -- r.con/r.coff are all-NULL in VALUES => Postgres types them TEXT; cast to int
         -- so COALESCE(.., v_on::int) unifies (review/dry-run fix).
         CASE WHEN r.freq = 'cyclical' THEN COALESCE(r.con::int,  v_on)  ELSE r.con::int  END,
         CASE WHEN r.freq = 'cyclical' THEN COALESCE(r.coff::int, v_off) ELSE r.coff::int END,
         r.sdate, r.edate, r.status, r.purpose, r.notes
  FROM (VALUES
    -- name, dose(per dose), unit, timing[], freq, cyc_on, cyc_off, start, end, status, purpose, notes
    ('metformin',            1000, 'mg', ARRAY['anytime']::text[],          'daily',    NULL, NULL, DATE '2026-01-01', DATE '2026-04-06', 'discontinued', 'insulin resistance', 'start_date APPROXIMATE (pre-Mar unknown) — confirm with PC; replaced by berberine'),
    ('berberine',             750, 'mg', ARRAY['lunch','dinner']::text[],   '2x_daily', NULL, NULL, DATE '2026-03-23', NULL,             'active',       'insulin resistance', '750mg x2 = 1500mg/day; split dose, ONE regimen row'),
    ('vitamin_d3',           5000, 'IU', ARRAY['morning']::text[],          'daily',    NULL, NULL, DATE '2026-03-23', NULL,             'active',       'core daily',         'with fat meal'),
    ('vitamin_k2_mk7',        100, 'mcg',ARRAY['morning']::text[],          'daily',    NULL, NULL, DATE '2026-03-23', NULL,             'active',       'core daily',         'co-dose w/ D3'),
    ('methylcobalamin_b12',   500, 'mcg',ARRAY['morning']::text[],          'daily',    NULL, NULL, DATE '2026-03-23', NULL,             'active',       'core daily',         NULL),
    ('magnesium_bisglycinate',300, 'mg', ARRAY['bedtime']::text[],          'daily',    NULL, NULL, DATE '2026-03-23', NULL,             'active',       'sleep/recovery',     '~300mg'),
    ('magnesium_citrate',     200, 'mg', ARRAY['morning']::text[],          'daily',    NULL, NULL, DATE '2026-03-23', NULL,             'active',       'core daily',         '~200mg'),
    ('omega_3',                 2, 'g',  ARRAY['morning']::text[],          'daily',    NULL, NULL, DATE '2026-03-23', NULL,             'active',       'core daily',         '>=2g/day EPA+DHA, with meals'),
    ('creatine',                5, 'g',  ARRAY['anytime']::text[],          'daily',    NULL, NULL, DATE '2026-03-23', NULL,             'active',       'performance',        NULL),
    ('nac',                   600, 'mg', ARRAY['dinner']::text[],           'daily',    NULL, NULL, DATE '2026-03-29', NULL,             'active',       'antioxidant',        NULL),
    ('nutritional_yeast',    NULL, 'g',  ARRAY['anytime']::text[],          'daily',    NULL, NULL, DATE '2026-03-23', NULL,             'active',       'food topping',       'B-vitamins'),
    -- VIOME box + probiotic + lozenges (since 2026-03-23)
    ('viome_precision_supplements', 8, 'caps', ARRAY['bedtime']::text[],    'daily',    NULL, NULL, DATE '2026-03-23', NULL,             'active',       'precision blend',    'compound; resolve actives via supplement_components'),
    ('viome_gut_formula',    12.9,'billion_cfu', ARRAY['bedtime']::text[],  'daily',    NULL, NULL, DATE '2026-03-23', NULL,             'active',       'gut',                'L. plantarum Lp-115'),
    ('viome_oral_lozenges',  NULL, 'lozenge', ARRAY['anytime']::text[],     'daily',    NULL, NULL, DATE '2026-03-23', NULL,             'active',       'oral microbiome',    NULL),
    -- START NOW (gate opened Mar 27; using today as definite start — CONFIRM with PC)
    ('ashwagandha_ksm66',     300, 'mg', ARRAY['dinner']::text[],           'cyclical', NULL, NULL, DATE '2026-06-02', NULL,             'active',       'stress/cortisol',    'cyclical 8-12wk on / 4wk off; nominal on/off from system_config; start=today CONFIRM'),
    ('zinc_picolinate',        27, 'mg', ARRAY['dinner']::text[],           'daily',    NULL, NULL, DATE '2026-06-02', NULL,             'active',       'immune/hormonal',    '25-30mg; start=today CONFIRM'),
    -- PLANNED (no start_date)
    ('collagen',               15, 'g',  ARRAY['anytime']::text[],          'daily',    NULL, NULL, NULL,              NULL,             'planned',      'connective tissue',  '10-20g pre-training; pair w/ vitamin C'),
    ('vitamin_c',             500, 'mg', ARRAY['anytime']::text[],          'daily',    NULL, NULL, NULL,              NULL,             'planned',      'collagen synthesis', 'pre-training'),
    ('nmn',                   500, 'mg', ARRAY['morning']::text[],          'daily',    NULL, NULL, NULL,              NULL,             'planned',      'NAD+',               'low priority'),
    ('resveratrol',           500, 'mg', ARRAY['anytime']::text[],          'daily',    NULL, NULL, NULL,              NULL,             'planned',      'longevity',          'standalone; low priority')
  ) AS r(name, dose, unit, timing, freq, con, coff, sdate, edate, status, purpose, notes)
  JOIN supplements s ON s.name = r.name
  ON CONFLICT (profile_id, supplement_id, COALESCE(start_date, '0001-01-01'::date)) DO NOTHING;

  -- ---- JOURNAL -> INTAKE BACKFILL (source='journal'), idempotent via UNIQUE ----
  -- Map ONLY real plan items (config-driven). took_fish_oil -> omega_3.
  -- took_calcium_supplement / took_electrolytes are NOT backfilled (no live plan item).
  INSERT INTO supplement_intake_logs (profile_id, supplement_id, taken_at, dose_amount, dose_unit, source, notes)
  SELECT jrn.profile_id, s.id, jrn.cycle_start, NULL, s.default_unit, 'journal',
         'backfilled from whoop_journal.took_fish_oil'
  FROM whoop_journal jrn
  JOIN supplements s ON s.name = 'omega_3'
  WHERE jrn.profile_id = v_pc_profile
    AND jrn.took_fish_oil IS TRUE
  ON CONFLICT (profile_id, supplement_id, taken_on, source) DO NOTHING;

END $$;

-- food_guidance seed (de-branded; global). Separate statement — not profile-scoped.
INSERT INTO food_guidance (item, classification, reason, alternative, category, scope, source) VALUES
 -- AVOID (never)
 ('coconut meat',    'avoid', 'Family-default avoid list (gut/metabolic)', NULL, 'fat',   'family', 'curated'),
 ('coconut milk',    'avoid', 'Family-default avoid list (gut/metabolic)', NULL, 'fat',   'family', 'curated'),
 ('coconut oil',     'avoid', 'Family-default avoid list (gut/metabolic)', 'olive oil', 'fat', 'family', 'curated'),
 ('coconut sugar',   'avoid', 'Family-default avoid list (gut/metabolic)', NULL, 'sweetener', 'family', 'curated'),
 ('coconut yogurt',  'avoid', 'Family-default avoid list (gut/metabolic)', NULL, 'dairy_alt', 'family', 'curated'),
 -- MINIMIZE (with alternative)
 ('ghee',            'minimize', 'Reduce saturated fat', 'olive oil',          'fat',    'family', 'curated'),
 ('paneer',          'minimize', 'Reduce; skip or minimal', 'skip / minimal',  'dairy',  'family', 'curated'),
 ('butter',          'minimize', 'Reduce; skip or minimal', 'skip / minimal',  'dairy',  'family', 'curated'),
 ('heavy cream',     'minimize', 'Reduce; skip or minimal', 'skip / minimal',  'dairy',  'family', 'curated'),
 ('pineapple',       'minimize', 'Occasional only',          'occasional',     'fruit',  'family', 'curated'),
 ('mango',           'minimize', 'Occasional only',          'occasional',     'fruit',  'family', 'curated'),
 ('lamb',            'minimize', 'Prefer leaner proteins',   'chicken / fish / eggs', 'meat', 'family', 'curated'),
 ('shallot',         'minimize', 'Onion is fine instead',    'onion',          'allium', 'family', 'curated'),
 ('rice noodles',    'minimize', 'Reduce or skip',           'reduce / skip',  'grain',  'family', 'curated'),
 -- SUPERFOOD (prioritise)
 ('olive oil',       'superfood', 'Prioritise', NULL, 'fat',       'family', 'curated'),
 ('eggs',            'superfood', 'Prioritise', NULL, 'protein',   'family', 'curated'),
 ('salmon',          'superfood', 'Prioritise', NULL, 'protein',   'family', 'curated'),
 ('garlic',          'superfood', 'Prioritise', NULL, 'allium',    'family', 'curated'),
 ('onion',           'superfood', 'Prioritise', NULL, 'allium',    'family', 'curated'),
 ('turmeric',        'superfood', 'Prioritise', NULL, 'spice',     'family', 'curated'),
 ('olives',          'superfood', 'Prioritise', NULL, 'fat',       'family', 'curated'),
 ('mushrooms',       'superfood', 'Prioritise', NULL, 'vegetable', 'family', 'curated')
ON CONFLICT (item, classification, scope) DO NOTHING;

-- =============================================================
-- H. CORRELATION-READINESS VIEWS  (ALL security_invoker = true)
--    Exposure is DATE-WINDOW based (NOT status). Discontinued/historical
--    regimens MUST appear for the dates they were active. CURRENT_DATE used as
--    the open-ended cap; never a hardcoded date.
-- =============================================================

-- -------------------------------------------------------------
-- H1. resolved_supplement_exposure  (regimen -> effective ingredient rows)
--     Expands compound products to their components so ingredient-level
--     correlation works. A regimen for the Viome box yields one row per
--     component (Ashwagandha, Zinc, ...) PLUS a row for the box itself.
--     A regimen for a standalone supplement yields just itself.
-- -------------------------------------------------------------
DROP VIEW IF EXISTS resolved_supplement_exposure CASCADE;
CREATE VIEW resolved_supplement_exposure
WITH (security_invoker = true) AS
-- the supplement as logged on the regimen (self)
SELECT r.id AS regimen_id, r.profile_id, r.supplement_id AS effective_supplement_id,
       r.start_date, r.end_date, r.status, r.frequency,
       r.cycle_days_on, r.cycle_days_off, false AS via_compound
FROM supplement_regimens r
UNION ALL
-- plus each ingredient of a compound product the regimen points at
SELECT r.id, r.profile_id, comp.component_id,
       r.start_date, r.end_date, r.status, r.frequency,
       r.cycle_days_on, r.cycle_days_off, true AS via_compound
FROM supplement_regimens r
JOIN supplement_components comp ON comp.product_id = r.supplement_id;
GRANT SELECT ON resolved_supplement_exposure TO authenticated;

-- -------------------------------------------------------------
-- H2. supplement_exposure_daily  (the core correlation spine)
--     One row per (profile, date, effective_supplement_id) where that supplement
--     was ACTIVELY EXPOSED on that date. Date spine = daily_health_summary dates
--     (so exposure aligns 1:1 with recovery/hrv outcomes). is_active is the
--     date-window verdict; on_phase handles cyclical on/off. days_since_start
--     makes dose-response ("recovery vs days since NAC start") trivial.
-- -------------------------------------------------------------
DROP VIEW IF EXISTS supplement_exposure_daily CASCADE;
CREATE VIEW supplement_exposure_daily
WITH (security_invoker = true) AS
SELECT
  d.profile_id,
  d.date,
  e.effective_supplement_id AS supplement_id,
  e.regimen_id,
  e.via_compound,
  (d.date - e.start_date)                       AS days_since_start,
  -- cyclical on/off: which day within the (on+off) cycle is this? on-phase if within first cycle_days_on
  CASE
    WHEN e.frequency = 'cyclical' AND e.cycle_days_on IS NOT NULL AND e.cycle_days_off IS NOT NULL
    THEN ((d.date - e.start_date) % (e.cycle_days_on + e.cycle_days_off)) < e.cycle_days_on
    ELSE true
  END                                            AS on_phase,
  true                                           AS is_active
FROM daily_health_summary d
JOIN resolved_supplement_exposure e
  ON e.profile_id = d.profile_id
 AND e.status <> 'planned'
 AND e.start_date IS NOT NULL
 AND d.date >= e.start_date
 AND d.date <= COALESCE(e.end_date, CURRENT_DATE);
-- FIX (review D1): do NOT filter cyclical OFF days out — exposure is the full
-- date window; on_phase (above) carries the ON/OFF signal so an ON-vs-OFF
-- GROUP BY on_phase is possible. Filtering OFF days made on_phase degenerate.
GRANT SELECT ON supplement_exposure_daily TO authenticated;

-- -------------------------------------------------------------
-- H3. daily_supplement_outcomes  (wide: outcomes + that day's exposure set)
--     Joins daily_health_summary outcomes to an aggregated array of the
--     supplements active that day. Drives "HRV on days X was ON vs OFF".
--     food_guidance vs food_rules PRECEDENCE is documented here:
--       a per-person food_rules row (001) for the same item OVERRIDES the
--       family-wide food_guidance default. The effective_food_guidance view
--       (H4) implements that override; daily_supplement_outcomes stays focused
--       on supplement exposure (food guidance is reference, not a daily outcome).
-- -------------------------------------------------------------
DROP VIEW IF EXISTS daily_supplement_outcomes CASCADE;
CREATE VIEW daily_supplement_outcomes
WITH (security_invoker = true) AS
SELECT
  d.profile_id,
  d.date,
  d.recovery_score_pct,
  d.hrv_ms,
  d.resting_hr_bpm,
  d.day_strain,
  d.sleep_performance_pct,
  d.asleep_duration_min,
  d.total_calories,
  d.sprint_name,
  COALESCE(ex.active_supplement_ids, ARRAY[]::uuid[]) AS active_supplement_ids,
  COALESCE(ex.active_supplement_names, ARRAY[]::text[]) AS active_supplement_names,
  COALESCE(ex.active_count, 0) AS active_supplement_count
FROM daily_health_summary d
LEFT JOIN (
  SELECT sed.profile_id, sed.date,
         array_agg(DISTINCT sed.supplement_id)      AS active_supplement_ids,
         array_agg(DISTINCT sup.name)               AS active_supplement_names,
         count(DISTINCT sed.supplement_id)          AS active_count
  FROM supplement_exposure_daily sed
  JOIN supplements sup ON sup.id = sed.supplement_id
  GROUP BY sed.profile_id, sed.date
) ex ON ex.profile_id = d.profile_id AND ex.date = d.date;
GRANT SELECT ON daily_supplement_outcomes TO authenticated;

-- -------------------------------------------------------------
-- H4. effective_food_guidance  (food_guidance overlaid with per-person food_rules)
--     Implements the PRECEDENCE rule: family-wide food_guidance is the default;
--     a per-person food_rules row for the same item WINS. Output is per-profile
--     (cross join guidance x profiles the caller can see), with override flagged.
--     security_invoker => food_rules RLS still applies (only your profiles' rules).
-- -------------------------------------------------------------
DROP VIEW IF EXISTS effective_food_guidance CASCADE;
CREATE VIEW effective_food_guidance
WITH (security_invoker = true) AS
SELECT
  p.id AS profile_id,
  fg.item,
  fg.classification         AS family_classification,
  fr.rule_type              AS personal_rule_type,
  fr.severity               AS personal_severity,
  CASE WHEN fr.id IS NOT NULL THEN 'personal_override' ELSE 'family_default' END AS effective_source,
  -- effective guidance: a personal allergy/intolerance/medical rule hardens to 'avoid';
  -- a personal 'preference' rule is advisory and keeps the family classification.
  CASE
    WHEN fr.rule_type IN ('allergy','intolerance','medical') THEN 'avoid'
    WHEN fr.rule_type = 'preference' THEN fg.classification
    ELSE fg.classification
  END                       AS effective_classification,
  fg.alternative,
  fg.reason                 AS family_reason,
  fr.notes                  AS personal_notes
FROM food_guidance fg
CROSS JOIN profiles p
LEFT JOIN food_rules fr
  ON fr.profile_id = p.id
 AND fr.is_active = true
 AND lower(fr.item) = lower(fg.item)
WHERE fg.is_active = true;
GRANT SELECT ON effective_food_guidance TO authenticated;

COMMIT;

-- =============================================================
-- EXAMPLE CORRELATION QUERIES (documentation; not executed by the migration)
-- =============================================================
-- 1) HRV during Berberine ON vs OFF (whole-period exposure contrast):
--   SELECT (s.name IS NOT NULL) AS berberine_on, round(avg(o.hrv_ms),1) AS avg_hrv, count(*) days
--   FROM daily_supplement_outcomes o
--   LEFT JOIN supplements s ON s.name='berberine' AND s.id = ANY(o.active_supplement_ids)
--   WHERE o.profile_id = '<PC profile>'
--   GROUP BY 1;
--
-- 2) Recovery vs days-since-NAC-start (dose-response over time):
--   SELECT sed.days_since_start, round(avg(o.recovery_score_pct),1) AS avg_recovery
--   FROM supplement_exposure_daily sed
--   JOIN supplements s ON s.id = sed.supplement_id AND s.name='nac'
--   JOIN daily_supplement_outcomes o ON o.profile_id=sed.profile_id AND o.date=sed.date
--   WHERE sed.profile_id = '<PC profile>'
--   GROUP BY sed.days_since_start ORDER BY sed.days_since_start;
--
-- 3) A biomarker (e.g. HbA1c) measured during Berberine window vs Metformin window
--    (biomarkers join on measured_at::date, not cycle dates):
--   SELECT r.status AS regimen_status, s.name, b.value AS hba1c, b.measured_at::date
--   FROM biomarkers b
--   JOIN metric_definitions m ON m.id=b.metric_definition_id AND m.name='hba1c'
--   JOIN supplement_regimens r ON r.profile_id=b.profile_id
--   JOIN supplements s ON s.id=r.supplement_id AND s.name IN ('berberine','metformin')
--   WHERE b.profile_id='<PC profile>'
--     AND b.measured_at::date BETWEEN r.start_date AND COALESCE(r.end_date, CURRENT_DATE);
