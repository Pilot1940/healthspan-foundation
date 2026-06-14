-- Migration 070: move remaining hardcoded thresholds into system_config (Rule #1)
--
-- Deep-scan 2026-06-14 found behaviour thresholds baked into code with no config key:
--   * brief.py + inbox_drain.py: `kcal < 0.6 * target` (minor "keep fuelling" nudge)
--   * brief.py: supplement timing-slot UTC hour boundaries (5/11/15/20)
--   * analysis/supplement_summary.py: 70% low-adherence cutoff
-- The code now reads these keys (with the same values as code-side fallbacks, so behaviour
-- is identical at the defaults). This migration seeds them so they are tunable without a deploy.
--
-- Config-only change; no schema/table/data touched. Idempotent (ON CONFLICT).

INSERT INTO public.system_config (key, value, description, category, is_active) VALUES
  ('brief.food_intake_threshold_pct', '0.6'::jsonb,
   'Minor daily-calorie fraction below which the brief/confirmation adds a "keep fuelling" nudge (growth framing). Used by monitor/brief.py + monitor/inbox_drain.py.',
   'brief', true),
  ('brief.supp_slot_morning_utc', '5'::jsonb,
   'UTC hour the brief''s supplement "morning" slot starts (slot grouping marker). monitor/brief.py.',
   'brief', true),
  ('brief.supp_slot_lunch_utc', '11'::jsonb,
   'UTC hour the brief''s supplement "lunch" slot starts. monitor/brief.py.',
   'brief', true),
  ('brief.supp_slot_dinner_utc', '15'::jsonb,
   'UTC hour the brief''s supplement "dinner" slot starts. monitor/brief.py.',
   'brief', true),
  ('brief.supp_slot_bedtime_utc', '20'::jsonb,
   'UTC hour the brief''s supplement "bedtime" slot starts. monitor/brief.py.',
   'brief', true),
  ('supplement.adherence_threshold_pct', '70'::jsonb,
   'Low-adherence cutoff (%) for analysis/supplement_summary.py — regimens below this are flagged.',
   'supplement', true)
ON CONFLICT (key) DO UPDATE
  SET description = EXCLUDED.description,
      is_active = true,
      updated_at = now();
