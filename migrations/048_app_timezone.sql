-- =============================================================
-- 048_app_timezone.sql
-- Seed the local timezone used for day boundaries + display.
--
-- Until now everything was UTC: maintainer_ingest_food sets
--   log_date = (logged_at AT TIME ZONE 'UTC')::date
-- and the brief filtered "today" by that UTC date. For a Thailand user the
-- day rolled over at 07:00 ICT, so 00:00–07:00 meals landed on the previous
-- day and times displayed in UTC (a 16:04 ICT meal looked like 09:04).
--
-- This seeds `app.timezone` (IANA name) in system_config. The brief now
-- computes "today" as a LOCAL-day window over the logged_at/taken_at
-- timestamptz columns (read-side, no data migration). No hardcoded tz in code
-- (CLAUDE rule #1). Change the value to move the household's zone.
-- =============================================================
BEGIN;

INSERT INTO public.system_config (key, value, description, category, is_active)
VALUES (
  'app.timezone',
  '"Asia/Bangkok"'::jsonb,
  'Local IANA timezone for day-boundary + display. The daily brief computes "today" in this zone.',
  'app',
  true
)
ON CONFLICT (key) DO UPDATE
  SET value = EXCLUDED.value,
      description = EXCLUDED.description,
      is_active = true,
      updated_at = now();

COMMIT;
