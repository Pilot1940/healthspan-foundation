-- =============================================================
-- 045_whoop_schema_expansion.sql
-- Add all missing WHOOP API fields across whoop_cycles, whoop_sleeps,
-- whoop_workouts, and create whoop_body_measurements for the body endpoint.
--
-- Approach: ADD COLUMN IF NOT EXISTS only — additive, no data loss,
-- no downtime on production. All new columns are nullable so existing
-- rows remain valid. Backfill runs after this migration via whoop_sync.py.
-- =============================================================
BEGIN;

-- ─────────────────────────────────────────────────────────────
-- 1. whoop_cycles — WHOOP v2 /v2/cycle + /v2/recovery fields
-- ─────────────────────────────────────────────────────────────
ALTER TABLE whoop_cycles
  ADD COLUMN IF NOT EXISTS score_state TEXT,                     -- SCORED / PENDING_SCORE / UNSCORABLE
  ADD COLUMN IF NOT EXISTS recovery_score_state TEXT,            -- recovery.score_state
  ADD COLUMN IF NOT EXISTS recovery_user_calibrating BOOLEAN,    -- recovery.score.user_calibrating
  ADD COLUMN IF NOT EXISTS sleep_cycle_count SMALLINT,           -- denorm from sleep.stage_summary
  ADD COLUMN IF NOT EXISTS disturbance_count SMALLINT,           -- denorm from sleep.stage_summary
  ADD COLUMN IF NOT EXISTS no_data_min NUMERIC(6,1),             -- denorm from sleep.stage_summary.total_no_data_time_milli
  ADD COLUMN IF NOT EXISTS whoop_updated_at TIMESTAMPTZ,         -- cycle.updated_at (WHOOP-side)
  ADD COLUMN IF NOT EXISTS whoop_created_at TIMESTAMPTZ;         -- cycle.created_at (WHOOP-side)

COMMENT ON COLUMN whoop_cycles.score_state IS
  'WHOOP scoring state: SCORED, PENDING_SCORE, or UNSCORABLE.';
COMMENT ON COLUMN whoop_cycles.recovery_user_calibrating IS
  'True during the first 30 days of WHOOP use — HRV baselines are not yet established.';

-- ─────────────────────────────────────────────────────────────
-- 2. whoop_sleeps — WHOOP v2 /v2/activity/sleep fields
-- ─────────────────────────────────────────────────────────────
ALTER TABLE whoop_sleeps
  ADD COLUMN IF NOT EXISTS score_state TEXT,            -- sleep.score_state
  ADD COLUMN IF NOT EXISTS no_data_min NUMERIC(6,1),    -- stage_summary.total_no_data_time_milli / 60000
  ADD COLUMN IF NOT EXISTS sleep_cycle_count SMALLINT,  -- stage_summary.sleep_cycle_count
  ADD COLUMN IF NOT EXISTS disturbance_count SMALLINT,  -- stage_summary.disturbance_count
  ADD COLUMN IF NOT EXISTS whoop_cycle_id BIGINT,       -- recovery.cycle_id (links sleep→cycle by WHOOP ID)
  ADD COLUMN IF NOT EXISTS whoop_updated_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS whoop_created_at TIMESTAMPTZ;

-- ─────────────────────────────────────────────────────────────
-- 3. whoop_workouts — WHOOP v2 /v2/activity/workout fields
-- ─────────────────────────────────────────────────────────────
ALTER TABLE whoop_workouts
  ADD COLUMN IF NOT EXISTS score_state TEXT,               -- workout.score_state
  ADD COLUMN IF NOT EXISTS sport_id SMALLINT,              -- workout.sport_id (integer catalogue key)
  ADD COLUMN IF NOT EXISTS percent_recorded NUMERIC(5,2),  -- score.percent_recorded (0–100)
  ADD COLUMN IF NOT EXISTS distance_m NUMERIC(8,1),        -- score.distance_meter
  ADD COLUMN IF NOT EXISTS altitude_gain_m NUMERIC(7,1),   -- score.altitude_gain_meter
  ADD COLUMN IF NOT EXISTS altitude_change_m NUMERIC(7,1), -- score.altitude_change_meter
  ADD COLUMN IF NOT EXISTS whoop_updated_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS whoop_created_at TIMESTAMPTZ;

COMMENT ON COLUMN whoop_workouts.sport_id IS
  'WHOOP sport catalogue integer (see _SPORT_NAMES in whoop_sync.py for mapping).';

-- ─────────────────────────────────────────────────────────────
-- 4. whoop_body_measurements — new, from GET /v2/user/measurement/body
-- One row per profile per calendar day (synced_date). Tracks weight,
-- height, and max HR over time as WHOOP updates them.
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS public.whoop_body_measurements (
  id          BIGSERIAL PRIMARY KEY,
  profile_id  UUID        NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  synced_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  synced_date DATE        NOT NULL,
  height_m    NUMERIC(4,2),
  weight_kg   NUMERIC(5,2),
  max_heart_rate SMALLINT,
  UNIQUE (profile_id, synced_date)
);

COMMENT ON TABLE public.whoop_body_measurements IS
  'Daily snapshot from WHOOP /v2/user/measurement/body. One row per profile per calendar day.';

-- ── RLS (required: every new table needs a policy in the same migration) ────
ALTER TABLE public.whoop_body_measurements ENABLE ROW LEVEL SECURITY;

-- Maintainer: read all rows
CREATE POLICY bm_maintainer_select ON public.whoop_body_measurements
  FOR SELECT USING (is_maintainer());

-- Profile owner: read own rows and write own rows
CREATE POLICY bm_owner_access ON public.whoop_body_measurements
  FOR ALL USING (has_profile_access(profile_id)) WITH CHECK (has_profile_access(profile_id));

-- ─────────────────────────────────────────────────────────────
-- 5. Verify
-- ─────────────────────────────────────────────────────────────
DO $$
BEGIN
  -- whoop_cycles new columns
  ASSERT (SELECT COUNT(*) FROM information_schema.columns
          WHERE table_schema='public' AND table_name='whoop_cycles'
            AND column_name = ANY(ARRAY['score_state','recovery_score_state',
                                        'recovery_user_calibrating','sleep_cycle_count',
                                        'disturbance_count','no_data_min',
                                        'whoop_updated_at','whoop_created_at'])) = 8,
    '045: whoop_cycles missing expected new columns';

  -- whoop_sleeps new columns
  ASSERT (SELECT COUNT(*) FROM information_schema.columns
          WHERE table_schema='public' AND table_name='whoop_sleeps'
            AND column_name = ANY(ARRAY['score_state','no_data_min','sleep_cycle_count',
                                        'disturbance_count','whoop_cycle_id',
                                        'whoop_updated_at','whoop_created_at'])) = 7,
    '045: whoop_sleeps missing expected new columns';

  -- whoop_workouts new columns
  ASSERT (SELECT COUNT(*) FROM information_schema.columns
          WHERE table_schema='public' AND table_name='whoop_workouts'
            AND column_name = ANY(ARRAY['score_state','sport_id','percent_recorded',
                                        'distance_m','altitude_gain_m','altitude_change_m',
                                        'whoop_updated_at','whoop_created_at'])) = 8,
    '045: whoop_workouts missing expected new columns';

  -- whoop_body_measurements exists
  ASSERT EXISTS (SELECT 1 FROM information_schema.tables
                 WHERE table_schema='public'
                   AND table_name='whoop_body_measurements'),
    '045: whoop_body_measurements table missing';

  RAISE NOTICE '045 verify: OK — WHOOP schema expansion applied';
END $$;

COMMIT;
