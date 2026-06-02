-- =============================================================
-- 002_multitenant_programs_ingestion.sql
-- Healthspan Foundation — RECONCILIATION migration
-- Adds: profiles/membership identity layer, training programs,
--       sync & ingestion observability, scoped per-tenant ingestion RPCs.
-- Reuses (EXTENDs, does NOT duplicate): user_goals, healthspan_tests,
--       biomarkers, wearable_sync_log, documents, stg_* staging.
-- Idempotent. Additive. Does NOT rewrite 001. Preserves existing data.
-- =============================================================

-- =============================================================
-- A. IDENTITY LAYER — profiles + access grants (the data key)
-- =============================================================
-- A profile is "a person whose data is tracked", independent of whether
-- they have an auth login. auth_user_id is NULLABLE (set if/when they log in).
CREATE TABLE IF NOT EXISTS profiles (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  auth_user_id  UUID UNIQUE REFERENCES auth.users(id) ON DELETE SET NULL, -- NULL for login-less members (kids)
  display_name  TEXT NOT NULL,
  date_of_birth DATE,
  sex           TEXT CHECK (sex IN ('male','female','other')),
  relationship  TEXT,            -- 'self','child','spouse','friend' (descriptive)
  is_active     BOOLEAN NOT NULL DEFAULT true,
  created_by    UUID REFERENCES auth.users(id) ON DELETE SET NULL,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE profiles ENABLE ROW LEVEL SECURITY;

CREATE TABLE IF NOT EXISTS profile_access (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  profile_id    UUID NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
  auth_user_id  UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  role          TEXT NOT NULL DEFAULT 'self' CHECK (role IN ('owner','guardian','self','viewer')),
  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (profile_id, auth_user_id)
);

ALTER TABLE profile_access ENABLE ROW LEVEL SECURITY;

-- STABLE SECURITY DEFINER helper: avoids per-row subquery recursion against
-- profile_access's own RLS, and lets every data-table policy be a 1-liner.
CREATE OR REPLACE FUNCTION can_access_profile(p_profile_id UUID)
RETURNS BOOLEAN
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
  SELECT EXISTS (
    SELECT 1 FROM profile_access pa
    WHERE pa.profile_id = p_profile_id
      AND pa.auth_user_id = auth.uid()
  );
$$;

-- profiles: visible if the caller has any grant to it; self-edit of own profile row
DROP POLICY IF EXISTS "profiles_access" ON profiles;
CREATE POLICY "profiles_access" ON profiles
  FOR ALL TO authenticated
  USING (can_access_profile(id))
  WITH CHECK (can_access_profile(id) OR created_by = auth.uid());

-- profile_access: a user sees grant rows that are theirs (base case, no recursion)
DROP POLICY IF EXISTS "profile_access_self" ON profile_access;
CREATE POLICY "profile_access_self" ON profile_access
  FOR ALL TO authenticated
  USING (auth_user_id = auth.uid())
  WITH CHECK (auth_user_id = auth.uid());

DROP TRIGGER IF EXISTS trg_profiles_updated_at ON profiles;
CREATE TRIGGER trg_profiles_updated_at
  BEFORE UPDATE ON profiles FOR EACH ROW EXECUTE FUNCTION update_updated_at();

-- -------------------------------------------------------------
-- A.1 Backfill: PC profile (auth_user_id = his existing UUID) + grant
-- -------------------------------------------------------------
INSERT INTO profiles (id, auth_user_id, display_name, relationship, created_by)
SELECT gen_random_uuid(), '0b0e4093-6758-46f7-a6e2-311ef6828a86',
       'PC', 'self', '0b0e4093-6758-46f7-a6e2-311ef6828a86'
WHERE NOT EXISTS (
  SELECT 1 FROM profiles WHERE auth_user_id = '0b0e4093-6758-46f7-a6e2-311ef6828a86'
);

INSERT INTO profile_access (profile_id, auth_user_id, role)
SELECT p.id, '0b0e4093-6758-46f7-a6e2-311ef6828a86', 'owner'
FROM profiles p
WHERE p.auth_user_id = '0b0e4093-6758-46f7-a6e2-311ef6828a86'
  AND NOT EXISTS (
    SELECT 1 FROM profile_access pa
    WHERE pa.profile_id = p.id AND pa.auth_user_id = '0b0e4093-6758-46f7-a6e2-311ef6828a86'
  );

-- Dea (14) + Dev (9): login-less child profiles, PC is guardian.
INSERT INTO profiles (id, auth_user_id, display_name, date_of_birth, sex, relationship, created_by)
SELECT gen_random_uuid(), NULL, 'Dea Singh Chitalkar', '2012-01-01', 'female', 'child',
       '0b0e4093-6758-46f7-a6e2-311ef6828a86'
WHERE NOT EXISTS (SELECT 1 FROM profiles WHERE display_name = 'Dea Singh Chitalkar');

INSERT INTO profiles (id, auth_user_id, display_name, date_of_birth, sex, relationship, created_by)
SELECT gen_random_uuid(), NULL, 'Dev Singh Chitalkar', '2017-01-01', 'male', 'child',
       '0b0e4093-6758-46f7-a6e2-311ef6828a86'
WHERE NOT EXISTS (SELECT 1 FROM profiles WHERE display_name = 'Dev Singh Chitalkar');

INSERT INTO profile_access (profile_id, auth_user_id, role)
SELECT p.id, '0b0e4093-6758-46f7-a6e2-311ef6828a86', 'guardian'
FROM profiles p
WHERE p.display_name IN ('Dea Singh Chitalkar','Dev Singh Chitalkar')
  AND NOT EXISTS (
    SELECT 1 FROM profile_access pa
    WHERE pa.profile_id = p.id AND pa.auth_user_id = '0b0e4093-6758-46f7-a6e2-311ef6828a86'
  );

-- =============================================================
-- B. ADD profile_id TO DATA TABLES (additive; user_id kept during transition)
-- =============================================================
-- Pattern repeated per table: add nullable profile_id, backfill from the PC
-- profile for existing rows, add FK + index. We do NOT drop user_id yet.
DO $$
DECLARE
  t TEXT;
  pc_profile UUID;
  data_tables TEXT[] := ARRAY[
    'weight_logs','body_metrics_history','biomarkers','daily_logs','daily_log_metrics',
    'food_logs','healthspan_tests','user_goals','biomarker_targets','food_rules',
    'documents','trend_alerts','wearable_sync_log','brain_conversations','brain_messages',
    'source_priority_config','user_locations','user_telegram_links','user_log_type_prefs',
    'sprints','whoop_cycles','whoop_sleeps','whoop_workouts','whoop_journal',
    'stg_biomarker_review','stg_food_rule_review','stg_test_result_review'
  ];
BEGIN
  SELECT id INTO pc_profile FROM profiles
  WHERE auth_user_id = '0b0e4093-6758-46f7-a6e2-311ef6828a86';

  FOREACH t IN ARRAY data_tables LOOP
    EXECUTE format('ALTER TABLE %I ADD COLUMN IF NOT EXISTS profile_id UUID', t);
    -- backfill: all existing rows belong to a user_id; map PC's user_id -> PC profile
    EXECUTE format($f$
      UPDATE %I SET profile_id = %L
      WHERE profile_id IS NULL
        AND user_id = '0b0e4093-6758-46f7-a6e2-311ef6828a86'
    $f$, t, pc_profile);
    -- add FK (guarded — ADD CONSTRAINT has no IF NOT EXISTS)
    IF NOT EXISTS (
      SELECT 1 FROM pg_constraint
      WHERE conname = format('%s_profile_id_fkey', t)
    ) THEN
      EXECUTE format(
        'ALTER TABLE %I ADD CONSTRAINT %I FOREIGN KEY (profile_id) REFERENCES profiles(id) ON DELETE CASCADE',
        t, format('%s_profile_id_fkey', t));
    END IF;
    EXECUTE format('CREATE INDEX IF NOT EXISTS %I ON %I (profile_id)',
      format('idx_%s_profile', t), t);
  END LOOP;
END $$;

-- -------------------------------------------------------------
-- B.1 Switch RLS to membership-based on all data tables.
--     (This intentionally REPLACES the founder-only USING(true) on the
--      5 Whoop+sprint tables — the inconsistency the task told us to fix.)
-- -------------------------------------------------------------
DO $$
DECLARE
  t TEXT;
  data_tables TEXT[] := ARRAY[
    'weight_logs','body_metrics_history','biomarkers','daily_logs','daily_log_metrics',
    'food_logs','healthspan_tests','user_goals','biomarker_targets','food_rules',
    'documents','trend_alerts','wearable_sync_log','brain_conversations','brain_messages',
    'source_priority_config','user_locations','user_telegram_links','user_log_type_prefs',
    'sprints','whoop_cycles','whoop_sleeps','whoop_workouts','whoop_journal',
    'stg_biomarker_review','stg_food_rule_review','stg_test_result_review'
  ];
BEGIN
  FOREACH t IN ARRAY data_tables LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    -- drop the old per-user/founder policies
    EXECUTE format('DROP POLICY IF EXISTS %I ON %I', t || '_isolation', t);
    EXECUTE format('DROP POLICY IF EXISTS %I ON %I', 'Users see own ' || t, t);
    -- new membership policy
    EXECUTE format('DROP POLICY IF EXISTS %I ON %I', t || '_profile_access', t);
    EXECUTE format($p$
      CREATE POLICY %I ON %I FOR ALL TO authenticated
      USING (can_access_profile(profile_id))
      WITH CHECK (can_access_profile(profile_id))
    $p$, t || '_profile_access', t);
  END LOOP;
END $$;

-- explicit drops of the 5 founder-only USING(true) policies by their real names
DROP POLICY IF EXISTS "Users see own sprints"  ON sprints;
DROP POLICY IF EXISTS "Users see own cycles"   ON whoop_cycles;
DROP POLICY IF EXISTS "Users see own sleeps"   ON whoop_sleeps;
DROP POLICY IF EXISTS "Users see own workouts" ON whoop_workouts;
DROP POLICY IF EXISTS "Users see own journal"  ON whoop_journal;

-- =============================================================
-- C. TRAINING PROGRAMS (WHY) — net-new. Orthogonal to sprints (WHERE+WHEN).
-- =============================================================
CREATE TABLE IF NOT EXISTS training_programs (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  profile_id    UUID NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
  name          TEXT NOT NULL,
  objective     TEXT,                         -- WHY: "VO2 Max for Mera Peak 6476m"
  target_event  TEXT,
  start_date    DATE,
  end_date      DATE,
  status        TEXT NOT NULL DEFAULT 'planned' CHECK (status IN ('planned','active','done','abandoned')),
  notes         TEXT,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
ALTER TABLE training_programs ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "training_programs_access" ON training_programs;
CREATE POLICY "training_programs_access" ON training_programs
  FOR ALL TO authenticated
  USING (can_access_profile(profile_id)) WITH CHECK (can_access_profile(profile_id));
DROP TRIGGER IF EXISTS trg_training_programs_updated_at ON training_programs;
CREATE TRIGGER trg_training_programs_updated_at
  BEFORE UPDATE ON training_programs FOR EACH ROW EXECUTE FUNCTION update_updated_at();
CREATE INDEX IF NOT EXISTS idx_training_programs_profile ON training_programs (profile_id, status);

CREATE TABLE IF NOT EXISTS program_phases (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  program_id    UUID NOT NULL REFERENCES training_programs(id) ON DELETE CASCADE,
  name          TEXT NOT NULL,                -- "Base Building"
  ordinal       INT NOT NULL DEFAULT 0,
  start_date    DATE,
  end_date      DATE,
  weekly_template JSONB DEFAULT '{}',
  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (program_id, ordinal)
);
ALTER TABLE program_phases ENABLE ROW LEVEL SECURITY;
-- RLS via parent program's profile (subquery; phases are low-cardinality)
DROP POLICY IF EXISTS "program_phases_access" ON program_phases;
CREATE POLICY "program_phases_access" ON program_phases
  FOR ALL TO authenticated
  USING (EXISTS (SELECT 1 FROM training_programs tp
                 WHERE tp.id = program_id AND can_access_profile(tp.profile_id)))
  WITH CHECK (EXISTS (SELECT 1 FROM training_programs tp
                 WHERE tp.id = program_id AND can_access_profile(tp.profile_id)));
CREATE INDEX IF NOT EXISTS idx_program_phases_program ON program_phases (program_id, ordinal);

-- JUNCTION: a workout belongs to ZERO-OR-MANY programs (explicit), and to ONE
-- sprint (auto, lives on whoop_workouts.sprint_id). This is many-to-many.
CREATE TABLE IF NOT EXISTS program_workouts (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  program_id    UUID NOT NULL REFERENCES training_programs(id) ON DELETE CASCADE,
  phase_id      UUID REFERENCES program_phases(id) ON DELETE SET NULL,
  workout_id    UUID NOT NULL REFERENCES whoop_workouts(id) ON DELETE CASCADE,
  workout_type  TEXT,                         -- 'vo2_max','zone2','strength'
  prescribed    BOOLEAN NOT NULL DEFAULT false, -- true = planned; false = executed/observed
  notes         TEXT,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (program_id, workout_id)
);
ALTER TABLE program_workouts ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "program_workouts_access" ON program_workouts;
CREATE POLICY "program_workouts_access" ON program_workouts
  FOR ALL TO authenticated
  USING (EXISTS (SELECT 1 FROM training_programs tp
                 WHERE tp.id = program_id AND can_access_profile(tp.profile_id)))
  WITH CHECK (EXISTS (SELECT 1 FROM training_programs tp
                 WHERE tp.id = program_id AND can_access_profile(tp.profile_id)));
CREATE INDEX IF NOT EXISTS idx_program_workouts_program ON program_workouts (program_id);
CREATE INDEX IF NOT EXISTS idx_program_workouts_workout ON program_workouts (workout_id);

-- =============================================================
-- D. REUSE/EXTEND existing tables instead of new health_goals /
--    health_investigations / biomarkers.investigation_id
-- =============================================================
-- D.1 user_goals  <- (was proposed: health_goals). Add program link + baseline.
ALTER TABLE user_goals ADD COLUMN IF NOT EXISTS program_id     UUID REFERENCES training_programs(id) ON DELETE SET NULL;
ALTER TABLE user_goals ADD COLUMN IF NOT EXISTS baseline_value NUMERIC;
ALTER TABLE user_goals ADD COLUMN IF NOT EXISTS achieved_value NUMERIC;
ALTER TABLE user_goals ADD COLUMN IF NOT EXISTS achieved_date  DATE;
CREATE INDEX IF NOT EXISTS idx_user_goals_program ON user_goals (program_id) WHERE program_id IS NOT NULL;

-- LANDMINE FIX: idx_user_goals_one_active enforced ONE active goal per user_id.
-- That breaks multi-goal programs AND the move to profile_id. Drop it; scope
-- uniqueness to (profile_id, metric_definition_id) active instead — a profile
-- can have many active goals, but not two active goals for the same metric.
DROP INDEX IF EXISTS idx_user_goals_one_active;
CREATE UNIQUE INDEX IF NOT EXISTS idx_user_goals_one_active_per_metric
  ON user_goals (profile_id, metric_definition_id) WHERE is_active = true;

-- D.2 healthspan_tests <- (was proposed: health_investigations).
ALTER TABLE healthspan_tests ADD COLUMN IF NOT EXISTS program_id UUID REFERENCES training_programs(id) ON DELETE SET NULL;
ALTER TABLE healthspan_tests ADD COLUMN IF NOT EXISTS type       TEXT;     -- 'blood','imaging','scan','genetic'
ALTER TABLE healthspan_tests ADD COLUMN IF NOT EXISTS provider   TEXT;
ALTER TABLE healthspan_tests ADD COLUMN IF NOT EXISTS location   TEXT;
-- Extend status CHECK to cover the investigation lifecycle (scheduled|done)
ALTER TABLE healthspan_tests DROP CONSTRAINT IF EXISTS healthspan_tests_status_check;
ALTER TABLE healthspan_tests ADD  CONSTRAINT healthspan_tests_status_check
  CHECK (status IN ('pending','complete','partial','scheduled','done'));
CREATE INDEX IF NOT EXISTS idx_healthspan_tests_program ON healthspan_tests (program_id) WHERE program_id IS NOT NULL;

-- D.3 biomarkers: link a result to its test EVENT (reuse, not new investigation_id).
ALTER TABLE biomarkers ADD COLUMN IF NOT EXISTS healthspan_test_id UUID REFERENCES healthspan_tests(id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS idx_biomarkers_test ON biomarkers (healthspan_test_id) WHERE healthspan_test_id IS NOT NULL;

-- D.4 whoop_workouts: free-form tags (proposed ALTER, kept).
ALTER TABLE whoop_workouts ADD COLUMN IF NOT EXISTS tags JSONB DEFAULT '[]';

-- =============================================================
-- E. SYNC & INGESTION OBSERVABILITY — EXTEND wearable_sync_log
-- =============================================================
ALTER TABLE wearable_sync_log ADD COLUMN IF NOT EXISTS method            TEXT
  CHECK (method IN ('csv','screenshot','photo','manual','api'));
ALTER TABLE wearable_sync_log ADD COLUMN IF NOT EXISTS records_in        INT DEFAULT 0;
ALTER TABLE wearable_sync_log ADD COLUMN IF NOT EXISTS records_upserted  INT DEFAULT 0;
ALTER TABLE wearable_sync_log ADD COLUMN IF NOT EXISTS records_skipped   INT DEFAULT 0;
ALTER TABLE wearable_sync_log ADD COLUMN IF NOT EXISTS records_failed    INT DEFAULT 0;
ALTER TABLE wearable_sync_log ADD COLUMN IF NOT EXISTS document_id       UUID REFERENCES documents(id) ON DELETE SET NULL;
ALTER TABLE wearable_sync_log ADD COLUMN IF NOT EXISTS source_path       TEXT;   -- S3 / storage path of the artifact
ALTER TABLE wearable_sync_log ADD COLUMN IF NOT EXISTS target_table      TEXT;   -- e.g. 'stg_food_log_review'

-- Per-record failure detail ("what failed") — child of a sync run.
CREATE TABLE IF NOT EXISTS sync_log_errors (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  sync_id       UUID NOT NULL REFERENCES wearable_sync_log(id) ON DELETE CASCADE,
  profile_id    UUID NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
  record_ref    TEXT,            -- e.g. CSV row #, cycle_start, food item name
  error_message TEXT NOT NULL,
  raw_payload   JSONB,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
ALTER TABLE sync_log_errors ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "sync_log_errors_access" ON sync_log_errors;
CREATE POLICY "sync_log_errors_access" ON sync_log_errors
  FOR ALL TO authenticated
  USING (can_access_profile(profile_id)) WITH CHECK (can_access_profile(profile_id));
CREATE INDEX IF NOT EXISTS idx_sync_log_errors_sync ON sync_log_errors (sync_id);

-- STAGING GAP FIX: food-photo nutrition is AI extraction -> must stage first
-- (rule 2). No staging table existed for food_logs nutrition. Add one,
-- mirroring the stg_* contract (confidence + status pending|approved|rejected|merged).
CREATE TABLE IF NOT EXISTS stg_food_log_review (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  profile_id    UUID NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
  document_id   UUID REFERENCES documents(id) ON DELETE SET NULL,
  sync_id       UUID REFERENCES wearable_sync_log(id) ON DELETE SET NULL,
  meal_type     TEXT,
  description   TEXT,
  calories      NUMERIC,
  protein_g     NUMERIC,
  carbs_g       NUMERIC,
  fat_g         NUMERIC,
  fiber_g       NUMERIC,
  foods         JSONB DEFAULT '[]',
  logged_at     TIMESTAMPTZ,
  confidence    NUMERIC CHECK (confidence >= 0 AND confidence <= 1),
  status        TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','rejected','merged')),
  reviewed_at   TIMESTAMPTZ,
  raw_text      TEXT,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
ALTER TABLE stg_food_log_review ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "stg_food_log_review_access" ON stg_food_log_review;
CREATE POLICY "stg_food_log_review_access" ON stg_food_log_review
  FOR ALL TO authenticated
  USING (can_access_profile(profile_id)) WITH CHECK (can_access_profile(profile_id));
CREATE INDEX IF NOT EXISTS idx_stg_food_log_review ON stg_food_log_review (profile_id, status);

-- =============================================================
-- F. PER-TENANT SCOPED INGESTION — token + SECURITY DEFINER RPC
-- =============================================================
-- A scoped ingestion token maps to exactly ONE profile. The skill embeds the
-- token (+ public anon key). The RPC resolves token->profile_id server-side;
-- the caller can NEVER write to a profile the token doesn't authorize.
-- Reuses the user_invite_tokens hashing pattern (store hash, not plaintext).
CREATE TABLE IF NOT EXISTS ingestion_tokens (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  profile_id    UUID NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
  token_hash    TEXT NOT NULL UNIQUE,        -- sha256(plaintext); plaintext shown once at mint
  label         TEXT,                        -- "Dea food-photo skill"
  allowed_methods TEXT[] NOT NULL DEFAULT ARRAY['photo','screenshot'],
  is_active     BOOLEAN NOT NULL DEFAULT true,
  expires_at    TIMESTAMPTZ,
  last_used_at  TIMESTAMPTZ,
  created_by    UUID REFERENCES auth.users(id) ON DELETE SET NULL,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
ALTER TABLE ingestion_tokens ENABLE ROW LEVEL SECURITY;
-- Only guardians/owners of the profile may see/manage its tokens. Never exposes hash use.
DROP POLICY IF EXISTS "ingestion_tokens_access" ON ingestion_tokens;
CREATE POLICY "ingestion_tokens_access" ON ingestion_tokens
  FOR ALL TO authenticated
  USING (can_access_profile(profile_id)) WITH CHECK (can_access_profile(profile_id));

-- Resolve a token to its profile, enforcing active/expiry/method. SECURITY DEFINER
-- so it can read ingestion_tokens regardless of caller role; returns NULL if invalid.
CREATE OR REPLACE FUNCTION resolve_ingestion_token(p_token TEXT, p_method TEXT)
RETURNS UUID
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = public AS $$
DECLARE
  v_profile UUID;
BEGIN
  SELECT profile_id INTO v_profile FROM ingestion_tokens
  WHERE token_hash = encode(digest(p_token, 'sha256'), 'hex')
    AND is_active = true
    AND (expires_at IS NULL OR expires_at > NOW())
    AND p_method = ANY(allowed_methods);
  IF v_profile IS NOT NULL THEN
    UPDATE ingestion_tokens SET last_used_at = NOW()
    WHERE token_hash = encode(digest(p_token, 'sha256'), 'hex');
  END IF;
  RETURN v_profile;  -- NULL => unauthorized
END;
$$;

-- Food-photo ingestion RPC. Writes to STAGING only (rule 2) + logs the run.
-- Token is the sole authority; no caller-supplied profile_id is trusted.
CREATE OR REPLACE FUNCTION ingest_food_photo(
  p_token TEXT,
  p_payload JSONB,         -- {description, calories, protein_g, ..., foods, logged_at, confidence, raw_text}
  p_source_path TEXT DEFAULT NULL
) RETURNS JSONB
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = public AS $$
DECLARE
  v_profile UUID;
  v_sync    UUID;
  v_stg     UUID;
BEGIN
  v_profile := resolve_ingestion_token(p_token, 'photo');
  IF v_profile IS NULL THEN
    RETURN jsonb_build_object('ok', false, 'error', 'unauthorized_token');
  END IF;

  INSERT INTO wearable_sync_log
    (user_id, profile_id, provider, sync_type, method, status, records_in, source_path, target_table, completed_at)
  VALUES
    ((SELECT auth_user_id FROM profiles WHERE id = v_profile), v_profile,
     'food_photo','incremental','photo','success', 1, p_source_path, 'stg_food_log_review', NOW())
  RETURNING id INTO v_sync;

  INSERT INTO stg_food_log_review
    (profile_id, sync_id, meal_type, description, calories, protein_g, carbs_g, fat_g, fiber_g,
     foods, logged_at, confidence, raw_text, status)
  VALUES
    (v_profile, v_sync,
     p_payload->>'meal_type', p_payload->>'description',
     (p_payload->>'calories')::NUMERIC, (p_payload->>'protein_g')::NUMERIC,
     (p_payload->>'carbs_g')::NUMERIC,  (p_payload->>'fat_g')::NUMERIC,
     (p_payload->>'fiber_g')::NUMERIC,  COALESCE(p_payload->'foods','[]'::jsonb),
     (p_payload->>'logged_at')::TIMESTAMPTZ, (p_payload->>'confidence')::NUMERIC,
     p_payload->>'raw_text', 'pending')
  RETURNING id INTO v_stg;

  RETURN jsonb_build_object('ok', true, 'staging_id', v_stg, 'sync_id', v_sync, 'profile_id', v_profile);
END;
$$;

-- Whoop screenshot ingestion RPC. Structured wearable data (not free-text AI
-- inference) is allowed to write whoop_cycles directly per a system_config
-- carve-out (key 'ingest.whoop_screenshot.direct_write'); otherwise stage.
CREATE OR REPLACE FUNCTION ingest_whoop_screenshot(
  p_token TEXT,
  p_payload JSONB,         -- {cycle_start, recovery_score_pct, hrv_ms, resting_hr_bpm, ...}
  p_source_path TEXT DEFAULT NULL
) RETURNS JSONB
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = public AS $$
DECLARE
  v_profile UUID; v_sync UUID; v_uid UUID; v_direct BOOLEAN;
BEGIN
  v_profile := resolve_ingestion_token(p_token, 'screenshot');
  IF v_profile IS NULL THEN
    RETURN jsonb_build_object('ok', false, 'error', 'unauthorized_token');
  END IF;
  SELECT auth_user_id INTO v_uid FROM profiles WHERE id = v_profile;
  SELECT COALESCE((value->>'enabled')::BOOLEAN, false) INTO v_direct
    FROM system_config WHERE key = 'ingest.whoop_screenshot.direct_write';

  INSERT INTO wearable_sync_log
    (user_id, profile_id, provider, sync_type, method, status, records_in, source_path,
     target_table, completed_at)
  VALUES
    (v_uid, v_profile, 'whoop','incremental','screenshot','success', 1, p_source_path,
     CASE WHEN v_direct THEN 'whoop_cycles' ELSE 'stg (deferred)' END, NOW())
  RETURNING id INTO v_sync;

  IF v_direct THEN
    INSERT INTO whoop_cycles
      (user_id, profile_id, cycle_start, recovery_score_pct, hrv_ms, resting_hr_bpm,
       blood_oxygen_pct, day_strain, source_file)
    VALUES
      (v_uid, v_profile, (p_payload->>'cycle_start')::TIMESTAMPTZ,
       (p_payload->>'recovery_score_pct')::NUMERIC, (p_payload->>'hrv_ms')::NUMERIC,
       (p_payload->>'resting_hr_bpm')::NUMERIC, (p_payload->>'blood_oxygen_pct')::NUMERIC,
       (p_payload->>'day_strain')::NUMERIC, p_source_path)
    ON CONFLICT (user_id, cycle_start) DO UPDATE
      SET recovery_score_pct = EXCLUDED.recovery_score_pct,
          hrv_ms = EXCLUDED.hrv_ms, resting_hr_bpm = EXCLUDED.resting_hr_bpm;
  END IF;

  RETURN jsonb_build_object('ok', true, 'sync_id', v_sync, 'profile_id', v_profile,
                            'wrote_direct', v_direct);
END;
$$;

-- Allow anon/authenticated to EXECUTE the RPCs (token is the gate, not the role).
GRANT EXECUTE ON FUNCTION ingest_food_photo(TEXT, JSONB, TEXT)        TO anon, authenticated;
GRANT EXECUTE ON FUNCTION ingest_whoop_screenshot(TEXT, JSONB, TEXT)  TO anon, authenticated;
REVOKE EXECUTE ON FUNCTION resolve_ingestion_token(TEXT, TEXT) FROM anon, authenticated; -- internal only

-- system_config seed for the whoop screenshot carve-out (rule 1: no hardcoded threshold)
INSERT INTO system_config (key, value, description, category)
SELECT 'ingest.whoop_screenshot.direct_write',
       '{"enabled": true}'::jsonb,
       'If true, structured Whoop screenshot fields write whoop_cycles directly (upsert). If false, defer to staging.',
       'ingestion'
WHERE NOT EXISTS (SELECT 1 FROM system_config WHERE key = 'ingest.whoop_screenshot.direct_write');
