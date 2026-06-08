-- =============================================================
-- 043_food_guidance_drop_old_unique.sql
--
-- Migration 004 created a table-level UNIQUE(item, classification, scope)
-- constraint on food_guidance (auto-named food_guidance_item_classification_scope_key).
--
-- Migration 041 added profile_id and partial per-profile unique indexes, but did
-- NOT drop the old table-level constraint. After 041 migrates all existing rows
-- to the maintainer profile (profile_id IS NOT NULL), that constraint will block
-- any attempt to insert a second profile's guidance for the same
-- item/classification/scope combination — silently breaking multi-profile Viome.
--
-- Fix:
--   1. Drop the old table-level unique constraint.
--   2. Add a replacement global partial index for rows where profile_id IS NULL
--      (future global guidance that applies to all profiles).
--   The per-profile partial index (food_guidance_personal_item_class) was already
--   created by migration 041 and covers profile_id IS NOT NULL rows.
-- =============================================================
BEGIN;

-- 1. Drop the old table-level unique constraint added in migration 004.
ALTER TABLE food_guidance
  DROP CONSTRAINT IF EXISTS food_guidance_item_classification_scope_key;

-- 2. Add replacement partial index for global rows (profile_id IS NULL).
--    Mirrors the design used for food_reference (global vs personal partial indexes).
CREATE UNIQUE INDEX IF NOT EXISTS food_guidance_global_item_class
  ON food_guidance (lower(item), classification, scope)
  WHERE profile_id IS NULL;

-- Verify
DO $$
BEGIN
  ASSERT NOT EXISTS (
    SELECT 1
    FROM information_schema.table_constraints
    WHERE table_schema = 'public'
      AND table_name   = 'food_guidance'
      AND constraint_name = 'food_guidance_item_classification_scope_key'
      AND constraint_type = 'UNIQUE'
  ), '043: old table-level unique constraint still present — drop failed';

  ASSERT EXISTS (
    SELECT 1 FROM pg_indexes
    WHERE schemaname = 'public'
      AND tablename  = 'food_guidance'
      AND indexname  = 'food_guidance_global_item_class'
  ), '043: food_guidance_global_item_class partial index missing';

  RAISE NOTICE '043 verify: OK — old UNIQUE constraint dropped, global partial index created';
END $$;

COMMIT;
