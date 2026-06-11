-- =============================================================
-- 064_food_reference_common_items.sql
-- BACKLOG #25: PC's frequent items kept triggering clarify/guess cycles because
-- the food_reference library either lacked the item or matched on the wrong
-- alias. food_reference matches a candidate by EXACT lower(name)/alias equality
-- (mig 041), so a brand spelled "Proten" never matched the "protein …" aliases.
--
-- This migration fixes the two GLOBAL branded rows (idempotent — safe on a fresh
-- instance and on PC's live DB):
--   1. Proten Thai Tea — set brand='Proten' and add the brand-accurate aliases
--      (proten / proten thai tea / …). Macros unchanged (190 kcal / 35P per 350 ml
--      bottle; verified against the 1/5-bottle log = 38 kcal ×5).
--   2. Hooray lactose-free Banana shake — a distinct flavour from the seeded
--      Hooray Strawberry row (different carbs: 15 vs 22), with banana-specific
--      aliases so it resolves to its own macros (220 kcal / 31P per 340 ml).
--
-- PC's PERSONAL morning shake ("TWT" = The Whole Truth whey + creatine + glutamine)
-- is profile-scoped and is seeded SEPARATELY (scripts/seed_food_ref_personal_064.py),
-- NOT here — same transportability rule as the strength seed (mig 063): no personal
-- data in a schema/library migration.
--
-- Apply: python3 scripts/hs_ops.py apply migrations/064_food_reference_common_items.sql
-- =============================================================
BEGIN;

-- ── 1. Proten Thai Tea — brand + aliases ──
-- Ensure the row exists (fresh instance), then set brand + the full alias set.

INSERT INTO public.food_reference
  (name, serving_desc, calories, protein_g, carbs_g, fat_g, fiber_g, brand,
   profile_id, source, verified, is_active, aliases)
SELECT 'Protein Thai Tea Shake', '1 bottle (350 ml)', 190, 35, 9, 2, 0, 'Proten',
       NULL, 'seed', true, true,
       ARRAY['thai tea shake','protein thai tea','thai tea protein shake',
             'thai milk tea protein shake','proten','proten thai tea',
             'proten thai tea shake','proten bottle','proten shake',
             'proten protein shake']
WHERE NOT EXISTS (
  SELECT 1 FROM public.food_reference
  WHERE lower(name) = 'protein thai tea shake' AND profile_id IS NULL);

UPDATE public.food_reference
SET brand = 'Proten',
    aliases = ARRAY['thai tea shake','protein thai tea','thai tea protein shake',
                    'thai milk tea protein shake','proten','proten thai tea',
                    'proten thai tea shake','proten bottle','proten shake',
                    'proten protein shake'],
    verified = true,
    updated_at = now()
WHERE lower(name) = 'protein thai tea shake' AND profile_id IS NULL;

-- ── 2. Hooray lactose-free Banana shake ──

INSERT INTO public.food_reference
  (name, serving_desc, calories, protein_g, carbs_g, fat_g, fiber_g, brand,
   profile_id, source, verified, is_active, aliases)
SELECT 'Hooray Banana Shake', '340 ml shake', 220, 31, 15, 3.5, 0, 'Hooray',
       NULL, 'seed', true, true,
       ARRAY['hooray banana','hooray banana shake','hooray lactose free banana',
             'hooray protein shake lactose free banana','hooray banana protein shake']
WHERE NOT EXISTS (
  SELECT 1 FROM public.food_reference
  WHERE lower(name) = 'hooray banana shake' AND profile_id IS NULL);

-- ── verify ──

DO $$
DECLARE
  v_proten int;
  v_banana int;
BEGIN
  SELECT count(*) INTO v_proten FROM public.food_reference
  WHERE profile_id IS NULL AND brand = 'Proten'
    AND 'proten' = ANY(SELECT lower(a) FROM unnest(aliases) AS a);
  IF v_proten <> 1 THEN
    RAISE EXCEPTION 'Migration 064 verify: Proten Thai Tea row not branded/aliased (found %)', v_proten;
  END IF;

  SELECT count(*) INTO v_banana FROM public.food_reference
  WHERE profile_id IS NULL AND lower(name) = 'hooray banana shake';
  IF v_banana <> 1 THEN
    RAISE EXCEPTION 'Migration 064 verify: Hooray Banana row missing (found %)', v_banana;
  END IF;

  RAISE NOTICE 'Migration 064 verify: OK — Proten Thai Tea aliased + Hooray Banana seeded';
END $$;

COMMIT;
