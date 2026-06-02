-- =============================================================
-- 006_travel_log.sql
-- Date-range TRAVEL LOG for location-based performance comparison.
-- Fills the gap sprints don't: sprints = 2 recent training blocks; travel_log =
-- all 15 trips (incl. 13 earlier: Tibet, Dubai x4, Spain, USA, Singapore...).
-- locations/user_locations are point-in-time "current location" markers (empty) —
-- they do NOT model trip date-ranges; travel_log does.
--
-- Hardened vs the source paste-and-run file:
--   + RLS (was missing — multi-tenant leak), + security_invoker view (was plain
--     view => RLS bypass), + idempotent seed (UNIQUE + ON CONFLICT), + profile via
--     relationship='self'.
-- NOTE: v_india_vs_travel depends on whoop_journal (a TABLE today). The later
--   journal table->view swap (007) must DROP/RECREATE this view in dependency order.
-- =============================================================
BEGIN;

CREATE TABLE IF NOT EXISTS public.travel_log (
    id               uuid DEFAULT gen_random_uuid() PRIMARY KEY,
    profile_id       uuid NOT NULL REFERENCES public.profiles(id),
    start_date       date NOT NULL,
    end_date         date NOT NULL,
    destination      text NOT NULL,
    country_code     text,
    alcohol_level    text CHECK (alcohol_level IN ('ZERO','Minimal','Moderate','Heavy')),
    activity_context text,
    is_altitude      boolean DEFAULT false,
    timezone_hint    text,
    notes            text,
    created_at       timestamptz DEFAULT now(),
    CONSTRAINT travel_log_dates_valid CHECK (end_date >= start_date),
    CONSTRAINT travel_log_uq UNIQUE (profile_id, start_date, destination)   -- idempotency
);
CREATE INDEX IF NOT EXISTS idx_travel_log_dates ON public.travel_log (profile_id, start_date, end_date);

-- RLS (foundation rule #3) — profile-scoped like every subject table
ALTER TABLE public.travel_log ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS travel_log_profile_access ON public.travel_log;
CREATE POLICY travel_log_profile_access ON public.travel_log
  FOR ALL TO authenticated
  USING (has_profile_access(profile_id))
  WITH CHECK (has_profile_access(profile_id));

-- Seed PC's 15 trips (idempotent)
DO $$
DECLARE p uuid;
BEGIN
    SELECT id INTO p FROM public.profiles WHERE relationship='self' LIMIT 1;
    IF p IS NULL THEN RAISE EXCEPTION 'No self profile found'; END IF;

    INSERT INTO public.travel_log (profile_id,start_date,end_date,destination,country_code,alcohol_level,activity_context,is_altitude,timezone_hint,notes) VALUES
    (p,'2025-04-19','2025-05-04','TIBET',     'CN','ZERO',    'Strenuous Trekking', true, 'Asia/Shanghai',    NULL),
    (p,'2025-05-11','2025-05-29','DUBAI',     'AE','ZERO',    'Relaxed',            false,'Asia/Dubai',       NULL),
    (p,'2025-06-05','2025-07-12','DUBAI',     'AE','Minimal', 'Summer with Family', false,'Asia/Dubai',       NULL),
    (p,'2025-08-26','2025-09-09','USA_CANADA','US','Heavy',   'Wedding Trip Rakesh',false,'America/New_York', NULL),
    (p,'2025-09-16','2025-09-27','DUBAI',     'AE','Moderate','Work',               false,'Asia/Dubai',       NULL),
    (p,'2025-09-30','2025-10-04','SINGAPORE', 'SG','Moderate','Work',               false,'Asia/Singapore',   NULL),
    (p,'2025-10-17','2025-11-01','SPAIN',     'ES','Moderate', NULL,                 false,'Europe/Madrid',    NULL),
    (p,'2025-11-02','2025-11-10','USA',       'US','Minimal',  NULL,                 false,'America/New_York', NULL),
    (p,'2025-11-23','2025-11-30','DUBAI',     'AE','Minimal',  NULL,                 false,'Asia/Dubai',       NULL),
    (p,'2025-12-13','2026-01-07','THAILAND',  'TH','ZERO',    'Active Time',        false,'Asia/Bangkok',     NULL),
    (p,'2026-02-01','2026-02-12','DUBAI',     'AE','ZERO',     NULL,                 false,'Asia/Dubai',       NULL),
    (p,'2026-02-19','2026-03-21','THAILAND',  'TH','Minimal', 'Active Time',        false,'Asia/Bangkok',     NULL),
    (p,'2026-04-08','2026-04-17','THAILAND',  'TH','ZERO',    'Active Time',        false,'Asia/Bangkok',     NULL),
    (p,'2026-05-13','2026-05-13','DUBAI',     'AE','ZERO',    'Transit',            false,'Asia/Dubai',       'One day transit'),
    (p,'2026-05-18','2026-05-29','NEPAL',     'NP','ZERO',    'Annapurna Trek',     true, 'Asia/Kathmandu',   'AMS at 4070M')
    ON CONFLICT (profile_id, start_date, destination) DO NOTHING;
END $$;

-- Location-performance comparison view (security_invoker => honours RLS)
CREATE OR REPLACE VIEW public.v_india_vs_travel
WITH (security_invoker = true) AS
SELECT
    COALESCE(t.destination, 'DELHI')        AS location,
    COALESCE(t.is_altitude, false)          AS is_altitude,
    CASE WHEN t.destination IS NULL THEN 'India' ELSE 'Non-India' END AS region,
    COALESCE(j.had_alcohol, false)          AS had_alcohol,
    CASE WHEN t.destination IS NULL THEN 'India' ELSE 'Non-India' END
        || ' — ' || CASE WHEN COALESCE(j.had_alcohol,false) THEN 'Alcohol' ELSE 'Dry' END AS analysis_group,
    c.profile_id,
    c.cycle_start::date                     AS cycle_date,
    c.recovery_score_pct, c.resting_hr_bpm, c.hrv_ms,
    c.sleep_performance_pct, c.asleep_duration_min,
    c.deep_sws_min, c.rem_min, c.sleep_efficiency_pct,
    c.day_strain, c.respiratory_rate_rpm,
    COALESCE(j.consumed_caffeine, false)    AS consumed_caffeine,
    COALESCE(j.consumed_added_sugar, false) AS consumed_added_sugar,
    COALESCE(j.ate_close_to_bedtime, false) AS ate_close_to_bedtime,
    COALESCE(j.spent_time_outdoors, false)  AS spent_time_outdoors,
    t.alcohol_level AS trip_alcohol_level,
    t.activity_context
FROM public.whoop_cycles c
LEFT JOIN public.travel_log t
    ON c.cycle_start::date BETWEEN t.start_date AND t.end_date
    AND c.profile_id = t.profile_id
LEFT JOIN public.whoop_journal j
    ON c.cycle_start::date = j.cycle_start::date
    AND c.profile_id = j.profile_id
WHERE c.recovery_score_pct IS NOT NULL;

GRANT SELECT ON public.v_india_vs_travel TO authenticated;

COMMIT;
