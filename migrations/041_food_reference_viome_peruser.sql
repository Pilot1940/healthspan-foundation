-- =============================================================
-- 041_food_reference_viome_peruser.sql
--
-- A. food_reference table — shared macro library.
--    profile_id IS NULL = global (visible to all authenticated users);
--    profile_id = <uuid> = user's personal food library entry.
--    Design mirrors food_guidance's new nullable profile_id (Section B).
--    Verified=true entries are curated/trusted (e.g. seeded Hooray);
--    verified=false entries are user-promoted via the learn-from-past flow.
--
-- B. food_guidance — add profile_id (nullable) so each user has their own
--    Viome guidance, not a shared family list. All 86 existing rows are PC's
--    personal Viome data; migrated to the maintainer profile here.
--    Future global guidance (applies to all) uses profile_id IS NULL.
--
-- C. effective_food_guidance view — updated to respect per-profile rows.
--    Added: AND (fg.profile_id IS NULL OR fg.profile_id = p.id)
--
-- D. SECURITY DEFINER RPCs for drain lookups:
--    lookup_food_reference, lookup_viome_verdicts, promote_food_to_reference
--
-- E. system_config threshold: food_reference.learn_min_logs
--
-- F. Seed: Hooray Strawberry Shake (global; profile_id IS NULL)
-- =============================================================
BEGIN;

-- =============================================================
-- A. food_reference
-- =============================================================

CREATE TABLE IF NOT EXISTS food_reference (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name         TEXT NOT NULL,
  aliases      TEXT[] NOT NULL DEFAULT '{}',
  serving_desc TEXT,
  calories     INTEGER NOT NULL,
  protein_g    NUMERIC(6,2) NOT NULL DEFAULT 0,
  carbs_g      NUMERIC(6,2) NOT NULL DEFAULT 0,
  fat_g        NUMERIC(6,2) NOT NULL DEFAULT 0,
  fiber_g      NUMERIC(6,2) DEFAULT 0,
  brand        TEXT,
  -- NULL = global (visible to all authenticated); UUID = personal entry
  profile_id   UUID REFERENCES profiles(id) ON DELETE CASCADE,
  source       TEXT NOT NULL DEFAULT 'manual',
  verified     BOOLEAN NOT NULL DEFAULT false,
  is_active    BOOLEAN NOT NULL DEFAULT true,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- One global entry per food name (case-insensitive); NULLs are distinct in
-- PG UNIQUE so we use partial indexes instead of a single table-level constraint.
CREATE UNIQUE INDEX IF NOT EXISTS food_reference_global_name
  ON food_reference (lower(name)) WHERE profile_id IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS food_reference_personal_name
  ON food_reference (lower(name), profile_id) WHERE profile_id IS NOT NULL;

DROP TRIGGER IF EXISTS trg_food_reference_updated_at ON food_reference;
CREATE TRIGGER trg_food_reference_updated_at
  BEFORE UPDATE ON food_reference
  FOR EACH ROW EXECUTE FUNCTION update_updated_at();

ALTER TABLE food_reference ENABLE ROW LEVEL SECURITY;

-- Read: global rows (profile_id IS NULL) visible to all authenticated;
--       personal rows visible to the owning profile's members.
DROP POLICY IF EXISTS food_reference_read ON food_reference;
CREATE POLICY food_reference_read ON food_reference
  FOR SELECT TO authenticated
  USING (profile_id IS NULL OR has_profile_access(profile_id));

-- Write: personal rows only via authenticated role.
--        Global rows (profile_id IS NULL) are service_role only — same as food_guidance.
DROP POLICY IF EXISTS food_reference_insert ON food_reference;
CREATE POLICY food_reference_insert ON food_reference
  FOR INSERT TO authenticated
  WITH CHECK (profile_id IS NOT NULL AND has_profile_access(profile_id));

DROP POLICY IF EXISTS food_reference_update ON food_reference;
CREATE POLICY food_reference_update ON food_reference
  FOR UPDATE TO authenticated
  USING (profile_id IS NOT NULL AND has_profile_access(profile_id))
  WITH CHECK (profile_id IS NOT NULL AND has_profile_access(profile_id));

GRANT SELECT ON food_reference TO authenticated;


-- =============================================================
-- B. food_guidance — add profile_id; migrate existing rows to maintainer
-- =============================================================

ALTER TABLE food_guidance
  ADD COLUMN IF NOT EXISTS profile_id UUID REFERENCES profiles(id) ON DELETE CASCADE;

-- Per-profile uniqueness mirrors the existing UNIQUE(item, classification, scope)
CREATE UNIQUE INDEX IF NOT EXISTS food_guidance_personal_item_class
  ON food_guidance (lower(item), classification, profile_id) WHERE profile_id IS NOT NULL;

-- Migrate: all existing rows are PC's Viome guidance; assign to the maintainer profile.
-- Uses profiles.is_maintainer (set in migration 022) — no hardcoded UUID.
DO $$
DECLARE
  v_maintainer UUID;
  v_count      INTEGER;
BEGIN
  SELECT id INTO v_maintainer FROM profiles WHERE is_maintainer = true LIMIT 1;
  IF v_maintainer IS NOT NULL THEN
    UPDATE food_guidance SET profile_id = v_maintainer WHERE profile_id IS NULL;
    GET DIAGNOSTICS v_count = ROW_COUNT;
    RAISE NOTICE '041: migrated % food_guidance rows to maintainer profile %',
      v_count, v_maintainer;
  ELSE
    RAISE NOTICE '041: WARNING — no maintainer profile found; food_guidance rows remain global';
  END IF;
END $$;

-- Update RLS: was USING (true) — that allowed all authenticated to see all rows.
-- Now: global rows (profile_id IS NULL) visible to all; personal rows to owner only.
DROP POLICY IF EXISTS food_guidance_read ON food_guidance;
CREATE POLICY food_guidance_read ON food_guidance
  FOR SELECT TO authenticated
  USING (profile_id IS NULL OR has_profile_access(profile_id));


-- =============================================================
-- C. effective_food_guidance view — per-profile filter
-- =============================================================

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
WHERE fg.is_active = true
  -- global rows apply to all profiles; personal rows only to their owner
  AND (fg.profile_id IS NULL OR fg.profile_id = p.id);

GRANT SELECT ON effective_food_guidance TO authenticated;


-- =============================================================
-- D. SECURITY DEFINER lookup RPCs
-- =============================================================

-- D1. lookup_food_reference
--     Returns the best macro match for a single candidate name.
--     Personal rows (profile_id = p_profile_id) take priority over global ones.
--     Wrapping in SECURITY DEFINER so the drain service account (healthspan.drainer)
--     bypasses RLS and sees both global + this profile's rows in one call.
CREATE OR REPLACE FUNCTION public.lookup_food_reference(
  p_name       TEXT,
  p_profile_id UUID
)
RETURNS TABLE (
  id           UUID,
  name         TEXT,
  calories     INTEGER,
  protein_g    NUMERIC,
  carbs_g      NUMERIC,
  fat_g        NUMERIC,
  fiber_g      NUMERIC,
  serving_desc TEXT,
  brand        TEXT,
  verified     BOOLEAN,
  is_global    BOOLEAN
)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path TO 'public', 'pg_catalog'
AS $$
  SELECT
    fr.id,
    fr.name,
    fr.calories,
    fr.protein_g,
    fr.carbs_g,
    fr.fat_g,
    fr.fiber_g,
    fr.serving_desc,
    fr.brand,
    fr.verified,
    (fr.profile_id IS NULL) AS is_global
  FROM food_reference fr
  WHERE fr.is_active = true
    AND (fr.profile_id IS NULL OR fr.profile_id = p_profile_id)
    AND (
      lower(fr.name) = lower(p_name)
      OR lower(p_name) = ANY(SELECT lower(a) FROM unnest(fr.aliases) AS a)
    )
  -- personal rows first (false < true), then global
  ORDER BY (fr.profile_id IS NULL) ASC
  LIMIT 1;
$$;


-- D2. lookup_viome_verdicts
--     Returns effective Viome guidance for a list of ingredient/food names for one profile.
--     Only returns rows with actionable classifications (avoid/minimize/superfood).
--     SECURITY DEFINER because effective_food_guidance is security_invoker=true; calling
--     it as the drainer service account would evaluate RLS as the drainer (wrong profile).
--     Wrapping here ensures the per-profile guidance is fetched correctly.
CREATE OR REPLACE FUNCTION public.lookup_viome_verdicts(
  p_items      TEXT[],
  p_profile_id UUID
)
RETURNS TABLE (
  item                     TEXT,
  effective_classification TEXT,
  reason                   TEXT
)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path TO 'public', 'pg_catalog'
AS $$
  SELECT
    efg.item,
    efg.effective_classification,
    COALESCE(efg.personal_notes, efg.family_reason) AS reason
  FROM effective_food_guidance efg
  WHERE efg.profile_id = p_profile_id
    AND efg.effective_classification IN ('avoid', 'minimize', 'superfood')
    AND lower(efg.item) = ANY(SELECT lower(i) FROM unnest(p_items) AS i)
  ORDER BY
    CASE efg.effective_classification
      WHEN 'avoid'    THEN 1
      WHEN 'minimize' THEN 2
      WHEN 'superfood' THEN 3
    END;
$$;


-- D3. promote_food_to_reference
--     Upsert a food (by name + profile) into food_reference at user scope.
--     Called by the learn-from-past flow once a webhook /learn command is wired up.
--     Returns the id of the inserted or updated row.
CREATE OR REPLACE FUNCTION public.promote_food_to_reference(
  p_name       TEXT,
  p_profile_id UUID,
  p_calories   INTEGER,
  p_protein_g  NUMERIC,
  p_carbs_g    NUMERIC,
  p_fat_g      NUMERIC,
  p_serving    TEXT DEFAULT NULL
)
RETURNS UUID
LANGUAGE plpgsql SECURITY DEFINER
SET search_path TO 'public', 'pg_catalog'
AS $$
DECLARE
  v_id UUID;
BEGIN
  SELECT id INTO v_id
  FROM food_reference
  WHERE profile_id = p_profile_id AND lower(name) = lower(p_name);

  IF FOUND THEN
    UPDATE food_reference
    SET calories   = p_calories,
        protein_g  = p_protein_g,
        carbs_g    = p_carbs_g,
        fat_g      = p_fat_g,
        updated_at = NOW()
    WHERE id = v_id;
  ELSE
    INSERT INTO food_reference
      (name, calories, protein_g, carbs_g, fat_g, serving_desc, profile_id, source, verified)
    VALUES
      (p_name, p_calories, p_protein_g, p_carbs_g, p_fat_g, p_serving, p_profile_id, 'learned', false)
    RETURNING id INTO v_id;
  END IF;
  RETURN v_id;
END;
$$;


-- =============================================================
-- E. system_config — learn-from-past threshold (Rule #1: no hardcoded values)
-- =============================================================

INSERT INTO system_config (key, value, description, category) VALUES
  ('food_reference.learn_min_logs', '2'::jsonb,
   'Minimum number of past confirmed food_logs with the same description before offering to promote the food to the user''s food_reference library.',
   'food')
ON CONFLICT (key) DO NOTHING;


-- =============================================================
-- F. Seed — Hooray Strawberry Shake (global; profile_id IS NULL)
-- =============================================================

INSERT INTO food_reference
  (name, aliases, serving_desc, calories, protein_g, carbs_g, fat_g, fiber_g, brand,
   profile_id, source, verified)
VALUES (
  'Hooray Strawberry Shake',
  ARRAY['hooray', 'hooray shake', 'hooray strawberry', 'hooray protein shake'],
  '340ml shake',
  250, 31, 22, 4, 0,
  'Hooray',
  NULL,  -- global
  'curated',
  true
);


-- =============================================================
-- Grants on functions
-- =============================================================

GRANT EXECUTE ON FUNCTION public.lookup_food_reference(TEXT, UUID) TO authenticated;
GRANT EXECUTE ON FUNCTION public.lookup_viome_verdicts(TEXT[], UUID) TO authenticated;
GRANT EXECUTE ON FUNCTION public.promote_food_to_reference(TEXT, UUID, INTEGER, NUMERIC, NUMERIC, NUMERIC, TEXT) TO authenticated;


-- =============================================================
-- Verify
-- =============================================================

DO $$
BEGIN
  ASSERT (SELECT count(*) FROM food_reference WHERE lower(name) = 'hooray strawberry shake' AND profile_id IS NULL) = 1,
    '041: Hooray global seed row missing';

  ASSERT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = 'food_guidance' AND column_name = 'profile_id'
  ), '041: food_guidance.profile_id column missing';

  ASSERT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = 'food_reference' AND column_name = 'aliases'
  ), '041: food_reference.aliases column missing';

  RAISE NOTICE '041 verify: OK — food_reference created, food_guidance migrated, Hooray seeded';
END $$;

COMMIT;
