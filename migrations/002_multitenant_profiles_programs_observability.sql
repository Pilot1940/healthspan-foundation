-- =============================================================
-- 002_multitenant_profiles_programs_observability.sql
-- Healthspan Foundation — multi-tenant profiles layer, training
-- programs, sync/ingestion observability, RLS reconciliation.
--
-- Extends the FOUNDATION track. Does NOT rewrite 001.
-- Postgres 15 (Supabase). Idempotent: IF NOT EXISTS / DROP ... IF EXISTS.
-- Preserves all existing data (572 whoop_cycles, 124 food_logs, etc.).
--
-- DESIGN: owner-vs-subject split.
--   user_id   = MANAGING auth user (who controls the row)  -- kept, additive
--   profile_id = SUBJECT person the data is ABOUT          -- new, what RLS uses
-- RLS predicate becomes: profile_id IN (SELECT accessible_profile_ids())
-- =============================================================

-- =============================================================
-- SECTION 0 — TENANCY: profiles + family_members
-- =============================================================

-- 0.1 profiles — one row per PERSON whose data is tracked.
-- auth_user_id is NULLABLE: set if/when the person gets a login (Dea),
-- NULL for managed-only subjects with no login (Dev).
CREATE TABLE IF NOT EXISTS profiles (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  auth_user_id    UUID UNIQUE REFERENCES auth.users(id) ON DELETE SET NULL,  -- nullable
  display_name    TEXT NOT NULL,
  date_of_birth   DATE,
  sex             TEXT CHECK (sex IN ('male', 'female', 'other')),
  relationship    TEXT,                       -- 'self','child','spouse','friend' (informational)
  is_minor        BOOLEAN NOT NULL DEFAULT false,
  created_by      UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE profiles ENABLE ROW LEVEL SECURITY;

DROP TRIGGER IF EXISTS trg_profiles_updated_at ON profiles;
CREATE TRIGGER trg_profiles_updated_at
  BEFORE UPDATE ON profiles
  FOR EACH ROW EXECUTE FUNCTION update_updated_at();

-- 0.2 family_members — the GRANT table. Maps an auth user to a profile
-- they may access, with a role. This is what accessible_profile_ids() reads.
CREATE TABLE IF NOT EXISTS family_members (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  profile_id  UUID NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
  auth_user_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  role        TEXT NOT NULL DEFAULT 'member' CHECK (role IN ('owner', 'member', 'child', 'viewer')),
  created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (profile_id, auth_user_id)
);

ALTER TABLE family_members ENABLE ROW LEVEL SECURITY;

CREATE INDEX IF NOT EXISTS idx_family_members_auth_user ON family_members (auth_user_id);
CREATE INDEX IF NOT EXISTS idx_family_members_profile   ON family_members (profile_id);

-- =============================================================
-- SECTION 1 — ACCESS HELPER (SECURITY DEFINER, no recursion)
-- =============================================================
-- Returns the set of profile ids the CURRENT auth.uid() may access:
--   (a) any profile they manage via family_members
--   (b) their OWN profile (profiles.auth_user_id = auth.uid())
-- SECURITY DEFINER so the function can read family_members/profiles
-- without being blocked by those tables' own RLS (avoids recursion).
CREATE OR REPLACE FUNCTION accessible_profile_ids()
RETURNS SETOF UUID
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public
AS $$
  SELECT fm.profile_id
    FROM family_members fm
   WHERE fm.auth_user_id = auth.uid()
  UNION
  SELECT p.id
    FROM profiles p
   WHERE p.auth_user_id = auth.uid();
$$;

REVOKE ALL ON FUNCTION accessible_profile_ids() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION accessible_profile_ids() TO authenticated;

-- Convenience: can the current user MANAGE (write) this profile?
-- owner/member/child roles can write; 'viewer' is read-only.
CREATE OR REPLACE FUNCTION can_write_profile(p_profile_id UUID)
RETURNS BOOLEAN
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public
AS $$
  SELECT EXISTS (
    SELECT 1 FROM family_members fm
     WHERE fm.auth_user_id = auth.uid()
       AND fm.profile_id = p_profile_id
       AND fm.role IN ('owner','member','child')
  ) OR EXISTS (
    SELECT 1 FROM profiles p
     WHERE p.id = p_profile_id
       AND p.auth_user_id = auth.uid()
  );
$$;

REVOKE ALL ON FUNCTION can_write_profile(UUID) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION can_write_profile(UUID) TO authenticated;

-- RLS for the tenancy tables themselves (now that the helper exists).
DROP POLICY IF EXISTS "profiles_access" ON profiles;
CREATE POLICY "profiles_access" ON profiles
  FOR ALL TO authenticated
  USING (id IN (SELECT accessible_profile_ids()))
  WITH CHECK (created_by = auth.uid());

DROP POLICY IF EXISTS "family_members_access" ON family_members;
CREATE POLICY "family_members_access" ON family_members
  FOR ALL TO authenticated
  USING (auth_user_id = auth.uid() OR profile_id IN (SELECT accessible_profile_ids()))
  WITH CHECK (auth_user_id = auth.uid() OR profile_id IN (SELECT accessible_profile_ids()));

-- =============================================================
-- SECTION 2 — ADD profile_id TO DATA TABLES (additive, idempotent)
-- =============================================================
-- We add profile_id NULLABLE first, backfill, then enforce NOT NULL.
-- Helper DO-block applies the column + FK + index to each data table.

DO $$
DECLARE
  t TEXT;
  data_tables TEXT[] := ARRAY[
    'biomarkers','body_metrics_history','daily_logs','daily_log_metrics',
    'food_logs','weight_logs','documents','healthspan_tests','user_goals',
    'biomarker_targets','food_rules','trend_alerts','brain_conversations',
    'brain_messages','source_priority_config','user_log_type_prefs',
    'user_locations','user_telegram_links','user_preference_history',
    'wearable_sync_log','stg_biomarker_review','stg_food_rule_review',
    'stg_test_result_review','sprints','whoop_cycles','whoop_sleeps',
    'whoop_workouts','whoop_journal'
  ];
BEGIN
  FOREACH t IN ARRAY data_tables LOOP
    EXECUTE format('ALTER TABLE %I ADD COLUMN IF NOT EXISTS profile_id UUID', t);
    -- FK (guard: only add if not already present)
    IF NOT EXISTS (
      SELECT 1 FROM information_schema.table_constraints
       WHERE table_name = t AND constraint_name = t || '_profile_id_fkey'
    ) THEN
      EXECUTE format(
        'ALTER TABLE %I ADD CONSTRAINT %I FOREIGN KEY (profile_id) REFERENCES profiles(id) ON DELETE CASCADE',
        t, t || '_profile_id_fkey');
    END IF;
    EXECUTE format('CREATE INDEX IF NOT EXISTS %I ON %I (profile_id)', 'idx_' || t || '_profile', t);
  END LOOP;
END $$;

-- whoop_* tables also lack a user_id FK (source DDL never added one). Add it.
DO $$
DECLARE
  t TEXT;
  whoop_tables TEXT[] := ARRAY['sprints','whoop_cycles','whoop_sleeps','whoop_workouts','whoop_journal'];
BEGIN
  FOREACH t IN ARRAY whoop_tables LOOP
    IF NOT EXISTS (
      SELECT 1 FROM information_schema.table_constraints
       WHERE table_name = t AND constraint_name = t || '_user_id_fkey'
    ) THEN
      EXECUTE format(
        'ALTER TABLE %I ADD CONSTRAINT %I FOREIGN KEY (user_id) REFERENCES auth.users(id) ON DELETE CASCADE',
        t, t || '_user_id_fkey');
    END IF;
  END LOOP;
END $$;

-- =============================================================
-- SECTION 3 — SEED Chitalkar family + BACKFILL profile_id
-- =============================================================
-- PC profile (self, has login). PC UUID is the existing auth user.
INSERT INTO profiles (id, auth_user_id, display_name, sex, relationship, is_minor, created_by)
VALUES (
  '00000000-0000-0000-0000-0000000000pc'::uuid,            -- deterministic PC profile id (placeholder, see note)
  '0b0e4093-6758-46f7-a6e2-311ef6828a86',
  'PC', 'male', 'self', false,
  '0b0e4093-6758-46f7-a6e2-311ef6828a86'
)
ON CONFLICT (auth_user_id) DO NOTHING;
-- NOTE: '...pc' is not a valid hex UUID — replace with gen_random_uuid()
-- captured into a psql variable at run time, OR remove the explicit id and
-- let the default fire. Kept explicit here only to show intent. See Risks.

-- Dea (14, will get a managed login -> auth_user_id set in a follow-up once
-- the auth user is created; NULL until then). Dev (9, no login).
INSERT INTO profiles (auth_user_id, display_name, date_of_birth, sex, relationship, is_minor, created_by)
SELECT NULL, 'Dea Singh Chitalkar', '2012-01-01'::date, 'female', 'child', true,
       '0b0e4093-6758-46f7-a6e2-311ef6828a86'
WHERE NOT EXISTS (SELECT 1 FROM profiles WHERE display_name = 'Dea Singh Chitalkar');

INSERT INTO profiles (auth_user_id, display_name, date_of_birth, sex, relationship, is_minor, created_by)
SELECT NULL, 'Dev Singh Chitalkar', '2017-01-01'::date, 'male', 'child', true,
       '0b0e4093-6758-46f7-a6e2-311ef6828a86'
WHERE NOT EXISTS (SELECT 1 FROM profiles WHERE display_name = 'Dev Singh Chitalkar');

-- Grant PC management over all three profiles.
INSERT INTO family_members (profile_id, auth_user_id, role)
SELECT p.id, '0b0e4093-6758-46f7-a6e2-311ef6828a86',
       CASE WHEN p.relationship = 'self' THEN 'owner' ELSE 'child' END
  FROM profiles p
 WHERE p.created_by = '0b0e4093-6758-46f7-a6e2-311ef6828a86'
ON CONFLICT (profile_id, auth_user_id) DO NOTHING;

-- Backfill: every EXISTING row belongs to PC's profile (all current data is PC's).
DO $$
DECLARE
  t TEXT;
  pc_profile UUID;
  data_tables TEXT[] := ARRAY[
    'biomarkers','body_metrics_history','daily_logs','daily_log_metrics',
    'food_logs','weight_logs','documents','healthspan_tests','user_goals',
    'biomarker_targets','food_rules','trend_alerts','brain_conversations',
    'brain_messages','source_priority_config','user_log_type_prefs',
    'user_locations','user_telegram_links','user_preference_history',
    'wearable_sync_log','stg_biomarker_review','stg_food_rule_review',
    'stg_test_result_review','sprints','whoop_cycles','whoop_sleeps',
    'whoop_workouts','whoop_journal'
  ];
BEGIN
  SELECT id INTO pc_profile FROM profiles
   WHERE auth_user_id = '0b0e4093-6758-46f7-a6e2-311ef6828a86';
  FOREACH t IN ARRAY data_tables LOOP
    EXECUTE format('UPDATE %I SET profile_id = $1 WHERE profile_id IS NULL', t)
      USING pc_profile;
  END LOOP;
END $$;

-- Now enforce NOT NULL on profile_id (data is backfilled).
DO $$
DECLARE
  t TEXT;
  data_tables TEXT[] := ARRAY[
    'biomarkers','body_metrics_history','daily_logs','daily_log_metrics',
    'food_logs','weight_logs','documents','healthspan_tests','user_goals',
    'biomarker_targets','food_rules','trend_alerts','brain_conversations',
    'brain_messages','source_priority_config','user_log_type_prefs',
    'user_locations','user_telegram_links','user_preference_history',
    'wearable_sync_log','stg_biomarker_review','stg_food_rule_review',
    'stg_test_result_review','sprints','whoop_cycles','whoop_sleeps',
    'whoop_workouts','whoop_journal'
  ];
BEGIN
  FOREACH t IN ARRAY data_tables LOOP
    EXECUTE format('ALTER TABLE %I ALTER COLUMN profile_id SET NOT NULL', t);
  END LOOP;
END $$;

-- =============================================================
-- SECTION 4 — REWRITE RLS: every data table -> profile_id predicate
-- =============================================================
-- This REPLACES the existing user_id = auth.uid() policies AND fixes the
-- broken USING(true) policies on the whoop_* + sprints tables.
-- Read (SELECT) = anyone with access; write = manager (can_write_profile).
DO $$
DECLARE
  t TEXT;
  data_tables TEXT[] := ARRAY[
    'biomarkers','body_metrics_history','daily_logs','daily_log_metrics',
    'food_logs','weight_logs','documents','healthspan_tests','user_goals',
    'biomarker_targets','food_rules','trend_alerts','brain_conversations',
    'brain_messages','source_priority_config','user_log_type_prefs',
    'user_locations','user_telegram_links','user_preference_history',
    'wearable_sync_log','stg_biomarker_review','stg_food_rule_review',
    'stg_test_result_review','sprints','whoop_cycles','whoop_sleeps',
    'whoop_workouts','whoop_journal'
  ];
  old_policy TEXT;
BEGIN
  FOREACH t IN ARRAY data_tables LOOP
    -- Drop ALL existing policies on the table (names vary; enumerate live).
    FOR old_policy IN
      SELECT policyname FROM pg_policies WHERE schemaname='public' AND tablename=t
    LOOP
      EXECUTE format('DROP POLICY IF EXISTS %I ON %I', old_policy, t);
    END LOOP;

    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);

    -- SELECT: any accessible profile
    EXECUTE format($f$
      CREATE POLICY %I ON %I FOR SELECT TO authenticated
      USING (profile_id IN (SELECT accessible_profile_ids()))
    $f$, t || '_sel', t);

    -- INSERT/UPDATE/DELETE: must be able to WRITE that profile
    EXECUTE format($f$
      CREATE POLICY %I ON %I FOR INSERT TO authenticated
      WITH CHECK (can_write_profile(profile_id))
    $f$, t || '_ins', t);

    EXECUTE format($f$
      CREATE POLICY %I ON %I FOR UPDATE TO authenticated
      USING (profile_id IN (SELECT accessible_profile_ids()))
      WITH CHECK (can_write_profile(profile_id))
    $f$, t || '_upd', t);

    EXECUTE format($f$
      CREATE POLICY %I ON %I FOR DELETE TO authenticated
      USING (can_write_profile(profile_id))
    $f$, t || '_del', t);
  END LOOP;
END $$;

-- Re-add the documents service_role shadow-read (was on the old table).
DROP POLICY IF EXISTS "documents_admin_shadow_read" ON documents;
CREATE POLICY "documents_admin_shadow_read" ON documents
  FOR SELECT TO service_role USING (true);

-- =============================================================
-- SECTION 5 — TRAINING PROGRAMS (NEW: program = WHY)
-- =============================================================
CREATE TABLE IF NOT EXISTS training_programs (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  profile_id    UUID NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
  user_id       UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,  -- managing owner
  name          TEXT NOT NULL,                  -- 'VO2 Max for Mera Peak 6476m'
  objective     TEXT,                           -- the WHY
  target_event  TEXT,                           -- 'Mera Peak summit'
  start_date    DATE,
  end_date      DATE,
  status        TEXT NOT NULL DEFAULT 'planned' CHECK (status IN ('planned','active','done','abandoned')),
  notes         TEXT,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
ALTER TABLE training_programs ENABLE ROW LEVEL SECURITY;
DROP TRIGGER IF EXISTS trg_training_programs_updated_at ON training_programs;
CREATE TRIGGER trg_training_programs_updated_at
  BEFORE UPDATE ON training_programs FOR EACH ROW EXECUTE FUNCTION update_updated_at();
CREATE INDEX IF NOT EXISTS idx_training_programs_profile ON training_programs (profile_id, status);

CREATE TABLE IF NOT EXISTS program_phases (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  program_id    UUID NOT NULL REFERENCES training_programs(id) ON DELETE CASCADE,
  profile_id    UUID NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,  -- denorm for RLS
  name          TEXT NOT NULL,                  -- 'Base Building'
  ordinal       INT NOT NULL DEFAULT 0,
  start_date    DATE,
  end_date      DATE,
  weekly_template JSONB DEFAULT '{}',
  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (program_id, ordinal)
);
ALTER TABLE program_phases ENABLE ROW LEVEL SECURITY;
CREATE INDEX IF NOT EXISTS idx_program_phases_program ON program_phases (program_id, ordinal);

-- Junction: a workout belongs to ZERO-OR-MANY programs (explicit).
CREATE TABLE IF NOT EXISTS program_workouts (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  program_id    UUID NOT NULL REFERENCES training_programs(id) ON DELETE CASCADE,
  phase_id      UUID REFERENCES program_phases(id) ON DELETE SET NULL,
  workout_id    UUID NOT NULL REFERENCES whoop_workouts(id) ON DELETE CASCADE,
  profile_id    UUID NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,  -- denorm for RLS
  workout_type  TEXT,                           -- 'vo2_max','zone2','strength'
  prescribed    BOOLEAN NOT NULL DEFAULT false, -- planned (true) vs after-the-fact tag (false)
  notes         TEXT,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (program_id, workout_id)
);
ALTER TABLE program_workouts ENABLE ROW LEVEL SECURITY;
CREATE INDEX IF NOT EXISTS idx_program_workouts_program ON program_workouts (program_id);
CREATE INDEX IF NOT EXISTS idx_program_workouts_workout ON program_workouts (workout_id);

-- RLS for the three program tables (same pattern as data tables).
DO $$
DECLARE t TEXT; prog_tables TEXT[] := ARRAY['training_programs','program_phases','program_workouts'];
BEGIN
  FOREACH t IN ARRAY prog_tables LOOP
    EXECUTE format('DROP POLICY IF EXISTS %I ON %I', t || '_sel', t);
    EXECUTE format('DROP POLICY IF EXISTS %I ON %I', t || '_ins', t);
    EXECUTE format('DROP POLICY IF EXISTS %I ON %I', t || '_upd', t);
    EXECUTE format('DROP POLICY IF EXISTS %I ON %I', t || '_del', t);
    EXECUTE format('CREATE POLICY %I ON %I FOR SELECT TO authenticated USING (profile_id IN (SELECT accessible_profile_ids()))', t || '_sel', t);
    EXECUTE format('CREATE POLICY %I ON %I FOR INSERT TO authenticated WITH CHECK (can_write_profile(profile_id))', t || '_ins', t);
    EXECUTE format('CREATE POLICY %I ON %I FOR UPDATE TO authenticated USING (profile_id IN (SELECT accessible_profile_ids())) WITH CHECK (can_write_profile(profile_id))', t || '_upd', t);
    EXECUTE format('CREATE POLICY %I ON %I FOR DELETE TO authenticated USING (can_write_profile(profile_id))', t || '_del', t);
  END LOOP;
END $$;

-- =============================================================
-- SECTION 6 — REUSE user_goals + EXTEND healthspan_tests
-- =============================================================
-- 6.1 user_goals REPLACES proposed health_goals. Add program linkage + fields.
ALTER TABLE user_goals ADD COLUMN IF NOT EXISTS program_id     UUID REFERENCES training_programs(id) ON DELETE SET NULL;
ALTER TABLE user_goals ADD COLUMN IF NOT EXISTS baseline_value NUMERIC;
ALTER TABLE user_goals ADD COLUMN IF NOT EXISTS achieved_value NUMERIC;
ALTER TABLE user_goals ADD COLUMN IF NOT EXISTS achieved_date  DATE;
-- NOTE: 001 has a partial unique index "one active goal per user"
--   (idx_user_goals_one_active ON user_goals(user_id) WHERE is_active).
-- Multi-goal-per-person is now required (a program has several goals).
-- Drop the single-active constraint.
DROP INDEX IF EXISTS idx_user_goals_one_active;

-- 6.2 healthspan_tests REPLACES proposed health_investigations.
-- Make test_definition_id NULLABLE (ad-hoc "Delhi Blood Panel" has no def)
-- and add investigation-style metadata + program linkage.
ALTER TABLE healthspan_tests ALTER COLUMN test_definition_id DROP NOT NULL;
ALTER TABLE healthspan_tests ADD COLUMN IF NOT EXISTS name          TEXT;     -- 'Delhi Blood Panel Jun 2026'
ALTER TABLE healthspan_tests ADD COLUMN IF NOT EXISTS investigation_type TEXT
  CHECK (investigation_type IS NULL OR investigation_type IN ('blood','imaging','scan','genetic','other'));
ALTER TABLE healthspan_tests ADD COLUMN IF NOT EXISTS provider      TEXT;
ALTER TABLE healthspan_tests ADD COLUMN IF NOT EXISTS location      TEXT;
ALTER TABLE healthspan_tests ADD COLUMN IF NOT EXISTS program_id    UUID REFERENCES training_programs(id) ON DELETE SET NULL;
-- widen status to include scheduled/pending (was: pending,complete,partial)
ALTER TABLE healthspan_tests DROP CONSTRAINT IF EXISTS healthspan_tests_status_check;
ALTER TABLE healthspan_tests ADD CONSTRAINT healthspan_tests_status_check
  CHECK (status IN ('scheduled','pending','partial','complete','cancelled'));

-- 6.3 biomarkers gets investigation linkage (proposed investigation_id).
ALTER TABLE biomarkers ADD COLUMN IF NOT EXISTS investigation_id UUID
  REFERENCES healthspan_tests(id) ON DELETE SET NULL;

-- 6.4 whoop_workouts.tags (proposed ALTER).
ALTER TABLE whoop_workouts ADD COLUMN IF NOT EXISTS tags JSONB DEFAULT '[]';

-- =============================================================
-- SECTION 7 — SYNC & INGESTION OBSERVABILITY
-- Extend wearable_sync_log (do NOT create a parallel table).
-- =============================================================
-- Widen semantics: covers csv/screenshot/photo/manual/api ingestion.
ALTER TABLE wearable_sync_log ADD COLUMN IF NOT EXISTS method TEXT
  CHECK (method IS NULL OR method IN ('csv','screenshot','photo','manual','api'));
ALTER TABLE wearable_sync_log ADD COLUMN IF NOT EXISTS document_id    UUID REFERENCES documents(id) ON DELETE SET NULL;
ALTER TABLE wearable_sync_log ADD COLUMN IF NOT EXISTS source_path    TEXT;          -- storage path / file
ALTER TABLE wearable_sync_log ADD COLUMN IF NOT EXISTS rows_in        INT DEFAULT 0; -- read from source
ALTER TABLE wearable_sync_log ADD COLUMN IF NOT EXISTS rows_upserted  INT DEFAULT 0;
ALTER TABLE wearable_sync_log ADD COLUMN IF NOT EXISTS rows_skipped   INT DEFAULT 0;
ALTER TABLE wearable_sync_log ADD COLUMN IF NOT EXISTS rows_failed    INT DEFAULT 0;
-- records_synced (existing) retained as a back-compat total.

-- Per-record error detail. A CHILD table, not JSONB — the ask is
-- "tell me what failed", so it must be queryable row-by-row.
CREATE TABLE IF NOT EXISTS sync_log_errors (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  sync_log_id   UUID NOT NULL REFERENCES wearable_sync_log(id) ON DELETE CASCADE,
  profile_id    UUID NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,  -- denorm for RLS
  record_ref    TEXT,            -- e.g. row number, cycle_start, photo region
  error_code    TEXT,
  error_message TEXT NOT NULL,
  raw_payload   JSONB,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
ALTER TABLE sync_log_errors ENABLE ROW LEVEL SECURITY;
CREATE INDEX IF NOT EXISTS idx_sync_log_errors_log ON sync_log_errors (sync_log_id);

DROP POLICY IF EXISTS "sync_log_errors_sel" ON sync_log_errors;
DROP POLICY IF EXISTS "sync_log_errors_ins" ON sync_log_errors;
DROP POLICY IF EXISTS "sync_log_errors_del" ON sync_log_errors;
CREATE POLICY "sync_log_errors_sel" ON sync_log_errors FOR SELECT TO authenticated
  USING (profile_id IN (SELECT accessible_profile_ids()));
CREATE POLICY "sync_log_errors_ins" ON sync_log_errors FOR INSERT TO authenticated
  WITH CHECK (can_write_profile(profile_id));
CREATE POLICY "sync_log_errors_del" ON sync_log_errors FOR DELETE TO authenticated
  USING (can_write_profile(profile_id));

-- Optional staging for food / whoop AI extraction (foundation rule 2:
-- AI output -> staging FIRST). 001 only had stg_biomarker/food_rule/test.
CREATE TABLE IF NOT EXISTS stg_food_log_review (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  profile_id    UUID NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
  user_id       UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  document_id   UUID REFERENCES documents(id) ON DELETE SET NULL,
  sync_log_id   UUID REFERENCES wearable_sync_log(id) ON DELETE SET NULL,
  meal_type     TEXT,
  description   TEXT,
  calories      NUMERIC, protein_g NUMERIC, carbs_g NUMERIC, fat_g NUMERIC,
  foods         JSONB DEFAULT '[]',
  logged_at     TIMESTAMPTZ,
  confidence    NUMERIC CHECK (confidence >= 0 AND confidence <= 1),
  status        TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','rejected','merged')),
  reviewed_at   TIMESTAMPTZ,
  raw_text      TEXT,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
ALTER TABLE stg_food_log_review ENABLE ROW LEVEL SECURITY;
CREATE INDEX IF NOT EXISTS idx_stg_food_log_review_profile ON stg_food_log_review (profile_id, status);
DROP POLICY IF EXISTS "stg_food_log_review_sel" ON stg_food_log_review;
DROP POLICY IF EXISTS "stg_food_log_review_ins" ON stg_food_log_review;
DROP POLICY IF EXISTS "stg_food_log_review_upd" ON stg_food_log_review;
CREATE POLICY "stg_food_log_review_sel" ON stg_food_log_review FOR SELECT TO authenticated
  USING (profile_id IN (SELECT accessible_profile_ids()));
CREATE POLICY "stg_food_log_review_ins" ON stg_food_log_review FOR INSERT TO authenticated
  WITH CHECK (can_write_profile(profile_id));
CREATE POLICY "stg_food_log_review_upd" ON stg_food_log_review FOR UPDATE TO authenticated
  USING (profile_id IN (SELECT accessible_profile_ids())) WITH CHECK (can_write_profile(profile_id));

-- =============================================================
-- SECTION 8 — PER-TENANT INGESTION RPC (SECURITY DEFINER, scoped)
-- =============================================================
-- Dea's .skill authenticates as HER auth session (her JWT, anon key).
-- This RPC lets a client stage a food/whoop extraction WITHOUT a service
-- key. It NEVER trusts a caller-supplied user; it confines the write to a
-- profile the CALLER may write (can_write_profile). A caller who passes a
-- profile_id they don't manage gets an exception — no god-mode.
CREATE OR REPLACE FUNCTION ingest_staged_food(
  p_profile_id  UUID,
  p_description  TEXT,
  p_logged_at    TIMESTAMPTZ,
  p_meal_type    TEXT DEFAULT NULL,
  p_calories     NUMERIC DEFAULT NULL,
  p_foods        JSONB DEFAULT '[]',
  p_confidence   NUMERIC DEFAULT NULL,
  p_document_id  UUID DEFAULT NULL,
  p_raw_text     TEXT DEFAULT NULL
)
RETURNS UUID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE v_id UUID;
BEGIN
  IF NOT can_write_profile(p_profile_id) THEN
    RAISE EXCEPTION 'access denied: caller % may not write profile %', auth.uid(), p_profile_id
      USING ERRCODE = '42501';
  END IF;
  INSERT INTO stg_food_log_review
    (profile_id, user_id, document_id, meal_type, description, calories,
     foods, logged_at, confidence, raw_text)
  VALUES
    (p_profile_id, auth.uid(), p_document_id, p_meal_type, p_description,
     p_calories, p_foods, p_logged_at, p_confidence, p_raw_text)
  RETURNING id INTO v_id;
  RETURN v_id;
END $$;
REVOKE ALL ON FUNCTION ingest_staged_food FROM PUBLIC;
GRANT EXECUTE ON FUNCTION ingest_staged_food TO authenticated;

-- Whoop screenshot ingestion is structured/high-confidence -> may upsert
-- whoop_cycles directly, but still profile-scoped and still logged.
CREATE OR REPLACE FUNCTION ingest_whoop_cycle(
  p_profile_id      UUID,
  p_cycle_start     TIMESTAMPTZ,
  p_recovery_pct    NUMERIC DEFAULT NULL,
  p_hrv_ms          NUMERIC DEFAULT NULL,
  p_rhr_bpm         NUMERIC DEFAULT NULL,
  p_day_strain      NUMERIC DEFAULT NULL,
  p_sleep_perf_pct  NUMERIC DEFAULT NULL,
  p_source          TEXT DEFAULT 'screenshot'
)
RETURNS UUID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE v_id UUID;
BEGIN
  IF NOT can_write_profile(p_profile_id) THEN
    RAISE EXCEPTION 'access denied: caller % may not write profile %', auth.uid(), p_profile_id
      USING ERRCODE = '42501';
  END IF;
  INSERT INTO whoop_cycles
    (profile_id, user_id, cycle_start, recovery_score_pct, hrv_ms,
     resting_hr_bpm, day_strain, sleep_performance_pct, source_file)
  VALUES
    (p_profile_id, auth.uid(), p_cycle_start, p_recovery_pct, p_hrv_ms,
     p_rhr_bpm, p_day_strain, p_sleep_perf_pct, p_source)
  ON CONFLICT (user_id, cycle_start) DO UPDATE SET
    recovery_score_pct    = COALESCE(EXCLUDED.recovery_score_pct, whoop_cycles.recovery_score_pct),
    hrv_ms                = COALESCE(EXCLUDED.hrv_ms, whoop_cycles.hrv_ms),
    resting_hr_bpm        = COALESCE(EXCLUDED.resting_hr_bpm, whoop_cycles.resting_hr_bpm),
    day_strain            = COALESCE(EXCLUDED.day_strain, whoop_cycles.day_strain),
    sleep_performance_pct = COALESCE(EXCLUDED.sleep_performance_pct, whoop_cycles.sleep_performance_pct)
  RETURNING id INTO v_id;
  RETURN v_id;
END $$;
REVOKE ALL ON FUNCTION ingest_whoop_cycle FROM PUBLIC;
GRANT EXECUTE ON FUNCTION ingest_whoop_cycle TO authenticated;
-- NOTE: the whoop_cycles UNIQUE is (user_id, cycle_start). For multi-tenant
-- correctness this should be (profile_id, cycle_start). See Risks (R5).

-- =============================================================
-- SECTION 9 — daily_health_summary VIEW: re-scope to profile_id
-- =============================================================
-- The view selects c.user_id; add profile_id so dashboards filter per subject.
CREATE OR REPLACE VIEW daily_health_summary AS
SELECT
  c.profile_id,
  c.user_id,
  c.cycle_start::date AS date,
  c.recovery_score_pct, c.hrv_ms, c.resting_hr_bpm, c.blood_oxygen_pct,
  c.day_strain, c.sleep_performance_pct, c.asleep_duration_min,
  c.deep_sws_min, c.rem_min, c.sleep_efficiency_pct, c.sleep_debt_min,
  (SELECT COUNT(*) FROM whoop_workouts w WHERE w.cycle_start = c.cycle_start AND w.profile_id = c.profile_id) AS workout_count,
  (SELECT SUM(w.duration_min) FROM whoop_workouts w WHERE w.cycle_start = c.cycle_start AND w.profile_id = c.profile_id) AS total_workout_min,
  (SELECT SUM(w.energy_burned_cal) FROM whoop_workouts w WHERE w.cycle_start = c.cycle_start AND w.profile_id = c.profile_id) AS total_workout_cal,
  j.had_alcohol, j.hydrated_sufficiently, j.consumed_added_sugar,
  (SELECT SUM(f.calories)  FROM food_logs f WHERE f.log_date = c.cycle_start::date AND f.profile_id = c.profile_id AND (f.is_day_summary IS NULL OR f.is_day_summary = false)) AS total_calories,
  (SELECT SUM(f.protein_g) FROM food_logs f WHERE f.log_date = c.cycle_start::date AND f.profile_id = c.profile_id AND (f.is_day_summary IS NULL OR f.is_day_summary = false)) AS total_protein_g,
  (SELECT SUM(f.carbs_g)   FROM food_logs f WHERE f.log_date = c.cycle_start::date AND f.profile_id = c.profile_id AND (f.is_day_summary IS NULL OR f.is_day_summary = false)) AS total_carbs_g,
  s.name AS sprint_name, s.slug AS sprint_slug
FROM whoop_cycles c
LEFT JOIN whoop_journal j ON j.profile_id = c.profile_id AND j.cycle_start = c.cycle_start
LEFT JOIN sprints s ON s.id = c.sprint_id
ORDER BY c.cycle_start DESC;

-- End of 002.
