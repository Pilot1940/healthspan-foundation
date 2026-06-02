-- =============================================================
-- 005_workout_detail.sql  (ADDITIVE)
-- HealthSpan: capture 100% of WHOOP workout detail + HR time-series.
--
-- GAP (from the WHOOP workout screen vs the DB): the CSV export only carries zone
-- PERCENTAGES + avg/max HR + strain + cals + duration. The app/API additionally have:
--   * per-zone DURATIONS (Z5 7:28, Z4 4:35, ...) and ZONE 0 (<110 bpm) — no columns
--   * CARDIO vs MUSCULAR load split (68/32) — no columns
--   * per-user ZONE HR BOUNDARIES (Z5 169+, Z4 158-168, ...) recalibrated periodically
--     (e.g. 2026-05-03) — user-level config, not per-workout
--   * the HR TIME-SERIES trace (ramp to ~175, recovery to ~100) — only via WHOOP API
-- max_hr_bpm already exists but the screenshot processor wasn't writing it (processor fix,
-- not schema). Zone durations matter most for VO2-max interval progression week-over-week.
-- =============================================================
BEGIN;

-- -------------------------------------------------------------
-- 1. Extend whoop_workouts: zone durations (seconds), zone 0, cardio/muscular split
--    Durations stored in SECONDS (screenshot is mm:ss; 7:28 -> 448) for precision.
-- -------------------------------------------------------------
ALTER TABLE whoop_workouts ADD COLUMN IF NOT EXISTS hr_zone0_pct  NUMERIC(5,2);  -- <110 bpm
ALTER TABLE whoop_workouts ADD COLUMN IF NOT EXISTS hr_zone0_sec  INTEGER;
ALTER TABLE whoop_workouts ADD COLUMN IF NOT EXISTS hr_zone1_sec  INTEGER;
ALTER TABLE whoop_workouts ADD COLUMN IF NOT EXISTS hr_zone2_sec  INTEGER;
ALTER TABLE whoop_workouts ADD COLUMN IF NOT EXISTS hr_zone3_sec  INTEGER;
ALTER TABLE whoop_workouts ADD COLUMN IF NOT EXISTS hr_zone4_sec  INTEGER;
ALTER TABLE whoop_workouts ADD COLUMN IF NOT EXISTS hr_zone5_sec  INTEGER;
ALTER TABLE whoop_workouts ADD COLUMN IF NOT EXISTS cardio_load_pct   NUMERIC(5,2);  -- 68
ALTER TABLE whoop_workouts ADD COLUMN IF NOT EXISTS muscular_load_pct NUMERIC(5,2);  -- 32
-- max_hr_bpm already exists (NULL today) — processor must populate it; no DDL change.

-- -------------------------------------------------------------
-- 2. hr_zone_config — per-profile zone boundaries, versioned by effective_date
--    WHOOP recalibrates zones; keep history so old workouts interpret against the
--    boundaries that were live then (look up by effective_date <= workout_start).
-- -------------------------------------------------------------
CREATE TABLE IF NOT EXISTS hr_zone_config (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  profile_id      UUID NOT NULL REFERENCES profiles(id),
  effective_date  DATE NOT NULL,
  max_hr_bpm      INTEGER,                 -- the max HR these zones were derived from
  z0_max_bpm      INTEGER,                 -- upper bound of Zone 0 (e.g. 109 => <110)
  z1_low_bpm      INTEGER, z1_high_bpm INTEGER,   -- 110-133
  z2_low_bpm      INTEGER, z2_high_bpm INTEGER,   -- 134-145
  z3_low_bpm      INTEGER, z3_high_bpm INTEGER,   -- 146-157
  z4_low_bpm      INTEGER, z4_high_bpm INTEGER,   -- 158-168
  z5_low_bpm      INTEGER,                          -- 169+
  source          TEXT NOT NULL DEFAULT 'whoop',
  notes           TEXT,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (profile_id, effective_date)
);
ALTER TABLE hr_zone_config ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS hr_zone_config_profile_access ON hr_zone_config;
CREATE POLICY hr_zone_config_profile_access ON hr_zone_config
  FOR ALL TO authenticated
  USING (has_profile_access(profile_id)) WITH CHECK (has_profile_access(profile_id));
CREATE INDEX IF NOT EXISTS idx_hr_zone_config_profile ON hr_zone_config (profile_id, effective_date DESC);

-- -------------------------------------------------------------
-- 3. workout_intervals — structured per-interval summary (the 90% case for VO2 work)
--    peak/recovery shape: time-to-peak, recovery drop, per-interval drift.
-- -------------------------------------------------------------
CREATE TABLE IF NOT EXISTS workout_intervals (
  id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  profile_id           UUID NOT NULL REFERENCES profiles(id),
  workout_id           UUID NOT NULL REFERENCES whoop_workouts(id) ON DELETE CASCADE,
  interval_index       INTEGER NOT NULL,         -- 1,2,3...
  kind                 TEXT CHECK (kind IN ('work','recovery','warmup','cooldown')),
  start_offset_sec     INTEGER,                  -- seconds from workout_start
  duration_sec         INTEGER,
  peak_hr_bpm          INTEGER,
  avg_hr_bpm           INTEGER,
  min_hr_bpm           INTEGER,
  time_to_peak_sec     INTEGER,
  recovery_hr_bpm      INTEGER,                  -- HR at end of following recovery
  recovery_drop_bpm    INTEGER,                  -- peak - recovery_hr (fitness signal)
  notes                TEXT,
  source               TEXT NOT NULL DEFAULT 'manual',  -- manual|screenshot|api
  created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (workout_id, interval_index)
);
ALTER TABLE workout_intervals ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS workout_intervals_profile_access ON workout_intervals;
CREATE POLICY workout_intervals_profile_access ON workout_intervals
  FOR ALL TO authenticated
  USING (has_profile_access(profile_id)) WITH CHECK (has_profile_access(profile_id));
CREATE INDEX IF NOT EXISTS idx_workout_intervals_workout ON workout_intervals (workout_id, interval_index);

-- -------------------------------------------------------------
-- 4. workout_hr_samples — raw HR time-series (the up/down trace). Source: WHOOP API.
--    ~1 sample / 5s => ~675 rows for a 56-min workout. Enables ramp-rate, recovery-rate,
--    peak-drift analysis. Compact columns; heavily indexed on (workout_id, t).
-- -------------------------------------------------------------
CREATE TABLE IF NOT EXISTS workout_hr_samples (
  id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  profile_id    UUID NOT NULL REFERENCES profiles(id),
  workout_id    UUID NOT NULL REFERENCES whoop_workouts(id) ON DELETE CASCADE,
  t_offset_sec  INTEGER NOT NULL,         -- seconds from workout_start
  hr_bpm        SMALLINT NOT NULL,
  UNIQUE (workout_id, t_offset_sec)
);
ALTER TABLE workout_hr_samples ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS workout_hr_samples_profile_access ON workout_hr_samples;
CREATE POLICY workout_hr_samples_profile_access ON workout_hr_samples
  FOR ALL TO authenticated
  USING (has_profile_access(profile_id)) WITH CHECK (has_profile_access(profile_id));
CREATE INDEX IF NOT EXISTS idx_workout_hr_samples_workout ON workout_hr_samples (workout_id, t_offset_sec);

-- -------------------------------------------------------------
-- 5. Seed PC's current zone boundaries (from screenshot; recalibrated 2026-05-03)
-- -------------------------------------------------------------
DO $$
DECLARE v_pc UUID;
BEGIN
  SELECT id INTO v_pc FROM profiles WHERE relationship='self' LIMIT 1;
  IF v_pc IS NOT NULL THEN
    INSERT INTO hr_zone_config (profile_id, effective_date, z0_max_bpm,
        z1_low_bpm,z1_high_bpm, z2_low_bpm,z2_high_bpm, z3_low_bpm,z3_high_bpm,
        z4_low_bpm,z4_high_bpm, z5_low_bpm, source, notes)
      VALUES (v_pc, '2026-05-03', 109, 110,133, 134,145, 146,157, 158,168, 169,
              'whoop', 'Zone ranges automatically updated 2026-05-03 (from workout screen).')
      ON CONFLICT (profile_id, effective_date) DO NOTHING;
  END IF;
END $$;

COMMIT;
