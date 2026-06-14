-- Migration 069: food→supplement bridge feature flag
--
-- A supplement named INSIDE a food log ("protein shake with creatine") is just text on
-- the food row — food and supplements are separate tables/write paths, and the drain
-- classifies a message as ONE kind, so the creatine never logged as an intake. The bridge
-- (monitor/inbox_drain.py) now also logs regimen supplements mentioned in food text as
-- intakes. This flag lets it be disabled without a code change (Rule #1: behaviour
-- toggles live in system_config, not hardcoded).
--
-- Scope is intentionally narrow: only the user's OWN ACTIVE regimen items match, with
-- whole-phrase matching, and nothing is learned. Idempotent via the existing
-- UNIQUE (profile_id, supplement_id, taken_on, source) upsert; the bridge pins taken_at
-- to noon-UTC of the day so it collapses with the 📝-menu row.

INSERT INTO public.system_config (key, value, description, category, is_active)
VALUES (
  'food_supplement_bridge.enabled',
  'true'::jsonb,
  'When true, the drain also logs the user''s active-regimen supplements that are named '
  'inside a food log (e.g. "shake with creatine") as supplement intakes. Regimen-scoped, '
  'whole-phrase match, no learning; idempotent + skips items already logged that day.',
  'ingest',
  true
)
ON CONFLICT (key) DO UPDATE
  SET description = EXCLUDED.description,
      is_active = true,
      updated_at = now();
