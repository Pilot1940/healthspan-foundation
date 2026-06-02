-- =============================================================
-- 002_multitenant_programs.sql
-- Extends the foundation track (001). Reconciliation, not greenfield.
-- Postgres 15 / Supabase. Idempotent (IF NOT EXISTS / DROP POLICY IF EXISTS).
-- Additive columns + careful ALTERs only. Preserves existing data
-- (572 whoop_cycles, 514 whoop_workouts, ~617 sleeps, ~410 journal, 68 biomarkers,
--  124 food_logs, 3 sprints). Touches DATA LAYER ONLY — auth.users untouched
-- (FK'd nullable). Foundation rule #6 (never modify auth logic) respected.
--
-- ⚠️ PRE-RUN VERIFICATION (cannot introspect live DB from authoring env):
--   Confirm the real constraint names for the whoop_* / daily_logs / sprints
--   UNIQUE constraints against information_schema.table_constraints BEFORE running
--   section F. The DROP CONSTRAINT names below are Postgres defaults; if the live
--   names differ, the DROP silently no-ops and the old user_id-scoped UNIQUE
--   survives, blocking two profiles from sharing a cycle_start. Query:
--     SELECT conname FROM pg_constraint
--     WHERE conrelid = 'whoop_cycles'::regclass AND contype = 'u';
-- =============================================================
BEGIN;

-- =============================================================
-- A. SUBJECT / TENANCY LAYER
--    profiles = one row per *person whose data is tracked*.
--    auth_user_id NULLABLE: NULL = managed-only subject (Dev, 9; Dea until she
--    gets a login). family_memberships = explicit auth_user -> profile grant
--    (direct grant, NOT family-transitive — 3 rows for PC, easy to reason about).
-- =============================================================
CREATE TABLE IF NOT EXISTS families (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name        TEXT NOT NULL,
  created_by  UUID REFERENCES auth.users(id) ON DELETE SET NULL,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS profiles (
  id                       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  auth_user_id             UUID UNIQUE REFERENCES auth.users(id) ON DELETE SET NULL, -- NULL = login-less
  family_id                UUID REFERENCES families(id) ON DELETE SET NULL,
  managed_by_auth_user_id  UUID REFERENCES auth.users(id) ON DELETE SET NULL,        -- who manages a login-less profile
  display_name             TEXT NOT NULL,
  date_of_birth            DATE,
  sex                      TEXT CHECK (sex IN ('male','female','other')),
  relationship             TEXT CHECK (relationship IN ('self','child','spouse','friend','other')),
  is_active                BOOLEAN NOT NULL DEFAULT true,
  created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at               TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS family_memberships (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  auth_user_id  UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  profile_id    UUID NOT NULL REFERENCES profiles(id)   ON DELETE CASCADE,
  role          TEXT NOT NULL DEFAULT 'self' CHECK (role IN ('owner','member','self')),
  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (auth_user_id, profile_id)
);

ALTER TABLE families           ENABLE ROW LEVEL SECURITY;
ALTER TABLE profiles           ENABLE ROW LEVEL SECURITY;
ALTER TABLE family_memberships ENABLE ROW LEVEL SECURITY;

DROP TRIGGER IF EXISTS trg_families_updated_at ON families;
CREATE TRIGGER trg_families_updated_at BEFORE UPDATE ON families
  FOR EACH ROW EXECUTE FUNCTION update_updated_at();
DROP TRIGGER IF EXISTS trg_profiles_updated_at ON profiles;
CREATE TRIGGER trg_profiles_updated_at BEFORE UPDATE ON profiles
  FOR EACH ROW EXECUTE FUNCTION update_updated_at();

CREATE INDEX IF NOT EXISTS idx_family_memberships_auth    ON family_memberships(auth_user_id);
CREATE INDEX IF NOT EXISTS idx_family_memberships_profile ON family_memberships(profile_id);

-- =============================================================
-- B. ACCESS HELPER (SECURITY DEFINER, pinned search_path)
--    MUST be SECURITY DEFINER: it reads family_memberships, whose own policy
--    is the non-recursive base case (auth_user_id = auth.uid()). Every data
--    table policy calls has_profile_access(profile_id) -> one cheap EXISTS.
--    Pinned search_path closes the SECURITY DEFINER injection vector.
-- =============================================================
CREATE OR REPLACE FUNCTION has_profile_access(p_profile_id UUID)
RETURNS BOOLEAN
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = public, pg_catalog
AS $$
  SELECT EXISTS (
    SELECT 1 FROM family_memberships
    WHERE auth_user_id = auth.uid() AND profile_id = p_profile_id
  );
$$;
REVOKE ALL ON FUNCTION has_profile_access(UUID) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION has_profile_access(UUID) TO authenticated;

-- membership/profile/family policies (non-recursive base cases)
DROP POLICY IF EXISTS family_memberships_access ON family_memberships;
CREATE POLICY family_memberships_access ON family_memberships FOR ALL TO authenticated
  USING (auth_user_id = auth.uid())
  WITH CHECK (auth_user_id = auth.uid());

DROP POLICY IF EXISTS profiles_access ON profiles;
CREATE POLICY profiles_access ON profiles FOR ALL TO authenticated
  USING (has_profile_access(id))
  WITH CHECK (has_profile_access(id));

DROP POLICY IF EXISTS families_access ON families;
CREATE POLICY families_access ON families FOR ALL TO authenticated
  USING (created_by = auth.uid() OR EXISTS (
           SELECT 1 FROM profiles p
           JOIN family_memberships m ON m.profile_id = p.id
           WHERE p.family_id = families.id AND m.auth_user_id = auth.uid()))
  WITH CHECK (created_by = auth.uid());

-- =============================================================
-- C. SEED Family "Chitalkar" (PC owner over all 3; Dea/Dev login-less)
-- =============================================================
DO $$
DECLARE v_family UUID; v_pc_profile UUID;
        v_pc CONSTANT UUID := '0b0e4093-6758-46f7-a6e2-311ef6828a86';
BEGIN
  SELECT id INTO v_family FROM families WHERE name='Chitalkar' AND created_by=v_pc LIMIT 1;
  IF v_family IS NULL THEN
    INSERT INTO families (name, created_by) VALUES ('Chitalkar', v_pc) RETURNING id INTO v_family;
  END IF;

  INSERT INTO profiles (auth_user_id, family_id, display_name, sex, relationship)
    VALUES (v_pc, v_family, 'PC', 'male', 'self')
    ON CONFLICT (auth_user_id) DO NOTHING;
  SELECT id INTO v_pc_profile FROM profiles WHERE auth_user_id = v_pc;

  IF NOT EXISTS (SELECT 1 FROM profiles WHERE family_id=v_family AND display_name='Dea Singh Chitalkar') THEN
    INSERT INTO profiles (auth_user_id, family_id, managed_by_auth_user_id, display_name, sex, relationship)
      VALUES (NULL, v_family, v_pc, 'Dea Singh Chitalkar', 'female', 'child');
  END IF;
  IF NOT EXISTS (SELECT 1 FROM profiles WHERE family_id=v_family AND display_name='Dev Singh Chitalkar') THEN
    INSERT INTO profiles (auth_user_id, family_id, managed_by_auth_user_id, display_name, sex, relationship)
      VALUES (NULL, v_family, v_pc, 'Dev Singh Chitalkar', 'male', 'child');
  END IF;

  -- PC owns all three profiles (explicit direct grants)
  INSERT INTO family_memberships (auth_user_id, profile_id, role)
    SELECT v_pc, p.id, 'owner' FROM profiles p WHERE p.family_id = v_family
    ON CONFLICT (auth_user_id, profile_id) DO NOTHING;
END $$;

-- =============================================================
-- D. RE-POINT SUBJECT TABLES TO profile_id (~22 tables)
--    Pattern per table: ADD profile_id -> backfill from user_id via profiles
--    -> SET NOT NULL -> DROP NOT NULL on user_id (login-less profiles have no
--    auth user) -> index. RLS swap is in section E (after backfill).
-- =============================================================
DO $$
DECLARE t TEXT;
        subject_tbls TEXT[] := ARRAY[
          'sprints','whoop_cycles','whoop_sleeps','whoop_workouts','whoop_journal',
          'biomarkers','food_logs','daily_logs','daily_log_metrics','weight_logs',
          'body_metrics_history','healthspan_tests','user_goals','food_rules',
          'biomarker_targets','documents','trend_alerts','wearable_sync_log',
          'stg_biomarker_review','stg_food_rule_review','stg_test_result_review'];
BEGIN
  FOREACH t IN ARRAY subject_tbls LOOP
    EXECUTE format('ALTER TABLE %I ADD COLUMN IF NOT EXISTS profile_id UUID REFERENCES profiles(id)', t);
    -- backfill: every existing row maps to the owner of its user_id (only PC has data today)
    EXECUTE format($f$
      UPDATE %I d SET profile_id = p.id
      FROM profiles p WHERE p.auth_user_id = d.user_id AND d.profile_id IS NULL
    $f$, t);
    EXECUTE format('ALTER TABLE %I ALTER COLUMN profile_id SET NOT NULL', t);
    -- login-less profiles (Dea/Dev) have no auth user; user_id must be droppable.
    -- profile_id is now the authoritative key. user_id kept (nullable) for rollback.
    EXECUTE format('ALTER TABLE %I ALTER COLUMN user_id DROP NOT NULL', t);
    EXECUTE format('CREATE INDEX IF NOT EXISTS idx_%s_profile ON %I(profile_id)', t, t);
  END LOOP;
END $$;

-- =============================================================
-- E. SWAP RLS POLICIES TO MEMBERSHIP (after backfill)
--    Drops the strict user_id policies AND the 5 founder-only USING(true)
--    Whoop/sprint leaks; reissues all as has_profile_access(profile_id).
--    Preserves the documents service_role shadow-read policy.
-- =============================================================
-- ROBUST FIX (review/advisor #1): the LIVE policy names differ from the source
-- DDL (live = users_own_whoop_cycles / users_own_sprints, NOT "Users see own…").
-- A name-matched DROP would no-op, leaving the USING(true) policy ALIVE; since PG
-- OR's permissive policies, that = true OR has_profile_access = TOTAL LEAK.
-- So we drop EVERY existing policy on each subject table by querying pg_policies,
-- regardless of its name. (documents service_role shadow-read is re-created below.)
DO $$
DECLARE t TEXT; p TEXT;
        subject_tbls TEXT[] := ARRAY[
          'sprints','whoop_cycles','whoop_sleeps','whoop_workouts','whoop_journal',
          'biomarkers','food_logs','daily_logs','daily_log_metrics','weight_logs',
          'body_metrics_history','healthspan_tests','user_goals','food_rules',
          'biomarker_targets','documents','trend_alerts','wearable_sync_log',
          'stg_biomarker_review','stg_food_rule_review','stg_test_result_review'];
BEGIN
  FOREACH t IN ARRAY subject_tbls LOOP
    -- drop ALL existing policies on this table (defends against any name drift)
    FOR p IN SELECT policyname FROM pg_policies WHERE schemaname='public' AND tablename=t LOOP
      EXECUTE format('DROP POLICY IF EXISTS %I ON %I', p, t);
    END LOOP;
    EXECUTE format($f$
      CREATE POLICY %I ON %I FOR ALL TO authenticated
        USING (has_profile_access(profile_id))
        WITH CHECK (has_profile_access(profile_id))
    $f$, t||'_profile_access', t);
  END LOOP;
END $$;

-- Preserve service_role shadow-read on documents (backend promotion path)
DROP POLICY IF EXISTS "documents_admin_shadow_read" ON documents;
CREATE POLICY "documents_admin_shadow_read" ON documents
  FOR SELECT TO service_role USING (true);

-- =============================================================
-- F. RE-KEY UNIQUE / PARTIAL-UNIQUE INDEXES (user_id -> profile_id)
--    ⚠️ Verify constraint names first (see header note). Two profiles must be
--    able to share a cycle_start / log_date.
-- =============================================================
ALTER TABLE whoop_cycles  DROP CONSTRAINT IF EXISTS whoop_cycles_user_id_cycle_start_key;
CREATE UNIQUE INDEX IF NOT EXISTS uq_whoop_cycles_profile  ON whoop_cycles (profile_id, cycle_start);
ALTER TABLE whoop_sleeps  DROP CONSTRAINT IF EXISTS whoop_sleeps_user_id_cycle_start_sleep_onset_key;
CREATE UNIQUE INDEX IF NOT EXISTS uq_whoop_sleeps_profile  ON whoop_sleeps (profile_id, cycle_start, sleep_onset);
ALTER TABLE whoop_workouts DROP CONSTRAINT IF EXISTS whoop_workouts_user_id_workout_start_key;
CREATE UNIQUE INDEX IF NOT EXISTS uq_whoop_workouts_profile ON whoop_workouts (profile_id, workout_start);
ALTER TABLE whoop_journal DROP CONSTRAINT IF EXISTS whoop_journal_user_id_cycle_start_key;
CREATE UNIQUE INDEX IF NOT EXISTS uq_whoop_journal_profile ON whoop_journal (profile_id, cycle_start);
ALTER TABLE sprints       DROP CONSTRAINT IF EXISTS sprints_user_id_slug_key;
CREATE UNIQUE INDEX IF NOT EXISTS uq_sprints_profile_slug  ON sprints (profile_id, slug);
ALTER TABLE daily_logs    DROP CONSTRAINT IF EXISTS daily_logs_user_id_log_date_log_type_key;
CREATE UNIQUE INDEX IF NOT EXISTS uq_daily_logs_profile    ON daily_logs (profile_id, log_date, log_type);

-- Partial-unique indexes from 001 keyed on user_id -> re-key to profile_id
DROP INDEX IF EXISTS idx_user_locations_one_current;
DROP INDEX IF EXISTS idx_user_telegram_links_one_current;
-- user_locations / user_telegram_links are ACCOUNT-scoped (stay on user_id) — recreate as-is
CREATE UNIQUE INDEX IF NOT EXISTS idx_user_locations_one_current
  ON user_locations (user_id) WHERE is_current = true;
CREATE UNIQUE INDEX IF NOT EXISTS idx_user_telegram_links_one_current
  ON user_telegram_links (user_id) WHERE is_current = true;

-- =============================================================
-- G. RE-KEY AUTO-SPRINT TRIGGERS TO profile_id (folded into 002, not deferred)
--    Triggers fire on prod whoop_*/food_logs inserts — i.e. when PC promotes
--    Dea's staged data. With Dea's user_id NULL and sprints keyed by profile_id,
--    matching on user_id would silently mis-tag (NULL). Rewrite to profile_id.
-- =============================================================
-- FIX (review #1): the live function is auto_tag_sprint(p_user_id UUID, ...).
-- CREATE OR REPLACE cannot rename an input parameter — must DROP first,
-- else the whole transaction aborts.
DROP FUNCTION IF EXISTS auto_tag_sprint(UUID, TIMESTAMPTZ);
CREATE OR REPLACE FUNCTION auto_tag_sprint(p_profile_id UUID, p_cycle_start TIMESTAMPTZ)
RETURNS UUID
LANGUAGE sql STABLE
SET search_path = public, pg_catalog
AS $$
  SELECT id FROM sprints
  WHERE profile_id = p_profile_id
    AND p_cycle_start::date BETWEEN start_date AND end_date
  ORDER BY start_date DESC
  LIMIT 1;
$$;

CREATE OR REPLACE FUNCTION trg_auto_tag_sprint()
RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = public, pg_catalog
AS $$
BEGIN
  IF NEW.sprint_id IS NULL THEN
    NEW.sprint_id := auto_tag_sprint(NEW.profile_id, NEW.cycle_start);
  END IF;
  RETURN NEW;
END $$;

CREATE OR REPLACE FUNCTION trg_auto_tag_sprint_food()
RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = public, pg_catalog
AS $$
BEGIN
  IF NEW.sprint_id IS NULL AND NEW.log_date IS NOT NULL THEN
    NEW.sprint_id := (
      SELECT id FROM sprints
      WHERE profile_id = NEW.profile_id
        AND NEW.log_date BETWEEN start_date AND end_date
      ORDER BY start_date DESC
      LIMIT 1);
  END IF;
  RETURN NEW;
END $$;
-- (Triggers themselves are unchanged — they already point at these functions.)

-- =============================================================
-- H. TRAINING PROGRAMS (the WHY dimension) — genuinely net-new
--    Sprint = WHERE+WHEN (auto, 1:1). Program = WHY (explicit, 0..N).
--    Workout belongs to ONE sprint (auto) and ZERO-OR-MANY programs (junction).
-- =============================================================
CREATE TABLE IF NOT EXISTS training_programs (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  profile_id   UUID NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
  name         TEXT NOT NULL,
  objective    TEXT,                                   -- the WHY
  target_event TEXT,                                   -- e.g. 'Mera Peak 6476m'
  start_date   DATE,
  end_date     DATE,
  status       TEXT NOT NULL DEFAULT 'planned' CHECK (status IN ('planned','active','done','abandoned')),
  created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
ALTER TABLE training_programs ENABLE ROW LEVEL SECURITY;
DROP TRIGGER IF EXISTS trg_training_programs_updated_at ON training_programs;
CREATE TRIGGER trg_training_programs_updated_at BEFORE UPDATE ON training_programs
  FOR EACH ROW EXECUTE FUNCTION update_updated_at();
DROP POLICY IF EXISTS training_programs_access ON training_programs;
CREATE POLICY training_programs_access ON training_programs FOR ALL TO authenticated
  USING (has_profile_access(profile_id))
  WITH CHECK (has_profile_access(profile_id));
CREATE INDEX IF NOT EXISTS idx_training_programs_profile ON training_programs(profile_id, status);

CREATE TABLE IF NOT EXISTS program_phases (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  program_id      UUID NOT NULL REFERENCES training_programs(id) ON DELETE CASCADE,
  name            TEXT NOT NULL,                        -- e.g. 'Base Building'
  ordinal         INT NOT NULL DEFAULT 0,
  start_date      DATE,
  end_date        DATE,
  weekly_template JSONB DEFAULT '{}',
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (program_id, ordinal)
);
ALTER TABLE program_phases ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS program_phases_access ON program_phases;
CREATE POLICY program_phases_access ON program_phases FOR ALL TO authenticated
  USING (EXISTS (SELECT 1 FROM training_programs tp
                 WHERE tp.id = program_id AND has_profile_access(tp.profile_id)))
  WITH CHECK (EXISTS (SELECT 1 FROM training_programs tp
                 WHERE tp.id = program_id AND has_profile_access(tp.profile_id)));
CREATE INDEX IF NOT EXISTS idx_program_phases_program ON program_phases(program_id, ordinal);

CREATE TABLE IF NOT EXISTS program_workouts (   -- junction: program <-> executed workout
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  program_id   UUID NOT NULL REFERENCES training_programs(id) ON DELETE CASCADE,
  phase_id     UUID REFERENCES program_phases(id) ON DELETE SET NULL,
  workout_id   UUID NOT NULL REFERENCES whoop_workouts(id) ON DELETE CASCADE,
  workout_type TEXT,                                    -- vo2_max | zone2 | strength
  prescribed   BOOLEAN NOT NULL DEFAULT false,          -- planned vs executed-and-attributed
  notes        TEXT,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (program_id, workout_id)
);
ALTER TABLE program_workouts ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS program_workouts_access ON program_workouts;
CREATE POLICY program_workouts_access ON program_workouts FOR ALL TO authenticated
  USING (EXISTS (SELECT 1 FROM training_programs tp
                 WHERE tp.id = program_id AND has_profile_access(tp.profile_id)))
  WITH CHECK (EXISTS (SELECT 1 FROM training_programs tp
                 WHERE tp.id = program_id AND has_profile_access(tp.profile_id)));
CREATE INDEX IF NOT EXISTS idx_program_workouts_program ON program_workouts(program_id);
CREATE INDEX IF NOT EXISTS idx_program_workouts_workout ON program_workouts(workout_id);

ALTER TABLE whoop_workouts ADD COLUMN IF NOT EXISTS tags JSONB DEFAULT '[]';

-- =============================================================
-- I. REUSE: user_goals (= proposed health_goals) — EXTEND, don't duplicate
-- =============================================================
ALTER TABLE user_goals ADD COLUMN IF NOT EXISTS program_id     UUID REFERENCES training_programs(id) ON DELETE SET NULL;
ALTER TABLE user_goals ADD COLUMN IF NOT EXISTS baseline_value NUMERIC;
ALTER TABLE user_goals ADD COLUMN IF NOT EXISTS achieved_value NUMERIC;
ALTER TABLE user_goals ADD COLUMN IF NOT EXISTS achieved_date  DATE;

-- Semantic fix: 001's idx_user_goals_one_active enforces ONE active goal per user.
-- Programs require concurrent goals (one standalone + one per active program).
DROP INDEX IF EXISTS idx_user_goals_one_active;
-- One active goal per (profile, program). NULL program_id => one standalone active goal.
CREATE UNIQUE INDEX IF NOT EXISTS uq_user_goals_one_active_per_program
  ON user_goals (profile_id, COALESCE(program_id, '00000000-0000-0000-0000-000000000000'::uuid))
  WHERE is_active = TRUE;

-- =============================================================
-- J. REUSE: healthspan_tests (= proposed health_investigations) — EXTEND
--    Files already live in documents(document_id). test_definitions/test_targets
--    already model the catalogue + reference ranges. No new investigations table.
-- =============================================================
ALTER TABLE healthspan_tests ADD COLUMN IF NOT EXISTS program_id UUID REFERENCES training_programs(id) ON DELETE SET NULL;
ALTER TABLE healthspan_tests ADD COLUMN IF NOT EXISTS provider   TEXT;
ALTER TABLE healthspan_tests ADD COLUMN IF NOT EXISTS location   TEXT;
ALTER TABLE healthspan_tests ADD COLUMN IF NOT EXISTS investigation_type TEXT
  CHECK (investigation_type IN ('blood','imaging','scan','genetic','other'));
ALTER TABLE healthspan_tests DROP CONSTRAINT IF EXISTS healthspan_tests_status_check;
ALTER TABLE healthspan_tests ADD  CONSTRAINT healthspan_tests_status_check
  CHECK (status IN ('scheduled','pending','complete','partial'));

-- biomarkers -> link to the test event that produced the value (closes a real gap)
ALTER TABLE biomarkers ADD COLUMN IF NOT EXISTS healthspan_test_id UUID REFERENCES healthspan_tests(id) ON DELETE SET NULL;

-- =============================================================
-- K. SYNC & INGESTION OBSERVABILITY — extend wearable_sync_log, add error child
--    Three existing layers reused: wearable_sync_log (run) + stg_* (payload pending)
--    + audit_log (promotion mutations). New: wearable_sync_errors (per-record).
-- =============================================================
ALTER TABLE wearable_sync_log ADD COLUMN IF NOT EXISTS method TEXT
  CHECK (method IN ('csv','screenshot','photo','manual','api'));
ALTER TABLE wearable_sync_log ADD COLUMN IF NOT EXISTS records_in       INT DEFAULT 0;
ALTER TABLE wearable_sync_log ADD COLUMN IF NOT EXISTS records_upserted INT DEFAULT 0;
ALTER TABLE wearable_sync_log ADD COLUMN IF NOT EXISTS records_skipped  INT DEFAULT 0;
ALTER TABLE wearable_sync_log ADD COLUMN IF NOT EXISTS records_failed   INT DEFAULT 0;
ALTER TABLE wearable_sync_log ADD COLUMN IF NOT EXISTS document_id      UUID REFERENCES documents(id) ON DELETE SET NULL;
ALTER TABLE wearable_sync_log ADD COLUMN IF NOT EXISTS source_path      TEXT;
-- (existing records_synced retained as back-compat alias for upserted)

CREATE TABLE IF NOT EXISTS wearable_sync_errors (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  sync_log_id   UUID NOT NULL REFERENCES wearable_sync_log(id) ON DELETE CASCADE,
  record_ref    TEXT,                                   -- CSV line / cycle_start / item
  error_code    TEXT,
  error_message TEXT NOT NULL,
  raw           JSONB,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
ALTER TABLE wearable_sync_errors ENABLE ROW LEVEL SECURITY;
CREATE INDEX IF NOT EXISTS idx_wearable_sync_errors_log ON wearable_sync_errors(sync_log_id);
DROP POLICY IF EXISTS wearable_sync_errors_access ON wearable_sync_errors;
CREATE POLICY wearable_sync_errors_access ON wearable_sync_errors FOR ALL TO authenticated
  USING (EXISTS (SELECT 1 FROM wearable_sync_log l
                 WHERE l.id = sync_log_id AND has_profile_access(l.profile_id)))
  WITH CHECK (EXISTS (SELECT 1 FROM wearable_sync_log l
                 WHERE l.id = sync_log_id AND has_profile_access(l.profile_id)));

-- Food-photo AI staging (no food-nutrition staging existed; rule #2 forces it)
CREATE TABLE IF NOT EXISTS stg_food_log_review (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  profile_id   UUID NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
  user_id      UUID REFERENCES auth.users(id) ON DELETE CASCADE,   -- nullable: login-less profiles
  document_id  UUID REFERENCES documents(id) ON DELETE SET NULL,
  sync_log_id  UUID REFERENCES wearable_sync_log(id) ON DELETE SET NULL,
  meal_type    TEXT,
  description  TEXT,
  calories     NUMERIC, protein_g NUMERIC, carbs_g NUMERIC, fat_g NUMERIC, fiber_g NUMERIC,
  foods        JSONB DEFAULT '[]',
  verdict      TEXT,
  confidence   NUMERIC CHECK (confidence >= 0 AND confidence <= 1),
  status       TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','rejected','merged')),
  reviewed_at  TIMESTAMPTZ,
  raw_text     TEXT,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
ALTER TABLE stg_food_log_review ENABLE ROW LEVEL SECURITY;
CREATE INDEX IF NOT EXISTS idx_stg_food_log_review_profile ON stg_food_log_review(profile_id, status);
DROP POLICY IF EXISTS stg_food_log_review_access ON stg_food_log_review;
CREATE POLICY stg_food_log_review_access ON stg_food_log_review FOR ALL TO authenticated
  USING (has_profile_access(profile_id))
  WITH CHECK (has_profile_access(profile_id));

-- system_config: route flags (rule #1 — no hardcoded behaviour). Whoop screenshots
-- default to staging (rule #2); flip to direct only by deliberate config change.
INSERT INTO system_config (key, value, description, category)
VALUES
 ('ingest.whoop_screenshot.direct_write', 'false'::jsonb,
  'If true, Whoop screenshot extraction may upsert whoop_* directly; else stages.', 'ingestion'),
 ('ingest.token.default_expiry_days', '90'::jsonb,
  'Default validity window for per-tenant ingestion tokens.', 'ingestion')
ON CONFLICT (key) DO NOTHING;

-- =============================================================
-- L. PER-TENANT INGESTION TOKENS + SECURITY DEFINER RPC
--    Dea's .skill embeds: anon (publishable) key + a per-profile ingestion token.
--    NO service-role key. The RPC resolves profile FROM the token (caller never
--    passes profile_id), writes to STAGING ONLY (rule #2), human-gated promotion.
-- =============================================================
CREATE TABLE IF NOT EXISTS ingestion_tokens (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  profile_id  UUID NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
  token_hash  BYTEA NOT NULL UNIQUE,                    -- digest(plaintext,'sha256'); plaintext never stored
  label       TEXT,
  allowed_methods TEXT[] NOT NULL DEFAULT ARRAY['photo','screenshot'],
  created_by  UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  expires_at  TIMESTAMPTZ,
  revoked_at  TIMESTAMPTZ,
  last_used_at TIMESTAMPTZ,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
ALTER TABLE ingestion_tokens ENABLE ROW LEVEL SECURITY;
-- Only the managing account may see/manage tokens; anon NEVER selects this table.
DROP POLICY IF EXISTS ingestion_tokens_access ON ingestion_tokens;
CREATE POLICY ingestion_tokens_access ON ingestion_tokens FOR ALL TO authenticated
  USING (created_by = auth.uid())
  WITH CHECK (created_by = auth.uid());

-- Admin RPC to mint a token (run by PC, authenticated). Returns plaintext ONCE.
CREATE OR REPLACE FUNCTION mint_ingestion_token(
  p_profile_id UUID, p_label TEXT DEFAULT NULL, p_methods TEXT[] DEFAULT ARRAY['photo','screenshot'])
RETURNS TEXT
LANGUAGE plpgsql SECURITY DEFINER
-- FIX (review #3): pgcrypto lives in the `extensions` schema on this Supabase
-- project (verified), so digest()/gen_random_bytes() must be on the path.
SET search_path = public, extensions, pg_catalog
AS $$
DECLARE v_plain TEXT; v_days INT;
BEGIN
  IF NOT has_profile_access(p_profile_id) THEN
    RAISE EXCEPTION 'access denied: cannot mint token for profile %', p_profile_id USING ERRCODE='42501';
  END IF;
  v_plain := 'ig_' || encode(gen_random_bytes(32), 'hex');
  -- FIX (review #2): system_config.value is JSONB — no direct jsonb->int cast.
  SELECT COALESCE((value #>> '{}')::int, 90) INTO v_days FROM system_config
    WHERE key = 'ingest.token.default_expiry_days';
  INSERT INTO ingestion_tokens (profile_id, token_hash, label, allowed_methods, created_by, expires_at)
  VALUES (p_profile_id, digest(v_plain,'sha256'), p_label, p_methods, auth.uid(), NOW() + make_interval(days => v_days));
  RETURN v_plain;  -- shown once; embed in the .skill
END $$;
REVOKE ALL ON FUNCTION mint_ingestion_token(UUID,TEXT,TEXT[]) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION mint_ingestion_token(UUID,TEXT,TEXT[]) TO authenticated;

-- The ingestion RPC: callable by anon. Token (not caller) selects the profile.
-- Writes profile_id as the authoritative key; user_id stays NULL for login-less
-- profiles (NEVER derives a non-null user_id from a login-less profile).
CREATE OR REPLACE FUNCTION ingest_health_artifact(
  p_token       TEXT,
  p_kind        TEXT,            -- 'food_photo' | 'whoop_screenshot'
  p_method      TEXT,            -- 'photo' | 'screenshot'
  p_source_path TEXT,
  p_payload     JSONB            -- array of extracted record objects
) RETURNS UUID
LANGUAGE plpgsql SECURITY DEFINER
-- FIX (review #3): pgcrypto in `extensions` schema — needed for digest().
SET search_path = public, extensions, pg_catalog
AS $$
DECLARE v_profile UUID; v_auth UUID; v_log UUID; v_rec JSONB;
        v_in INT := 0; v_ok INT := 0; v_fail INT := 0;
        v_methods TEXT[];
BEGIN
  SELECT profile_id, allowed_methods INTO v_profile, v_methods
    FROM ingestion_tokens
   WHERE token_hash = digest(p_token,'sha256')
     AND revoked_at IS NULL
     AND (expires_at IS NULL OR expires_at > NOW());
  IF v_profile IS NULL THEN RAISE EXCEPTION 'invalid or expired ingestion token' USING ERRCODE='42501'; END IF;
  IF NOT (p_method = ANY(v_methods)) THEN RAISE EXCEPTION 'method % not allowed for this token', p_method USING ERRCODE='42501'; END IF;
  IF p_kind NOT IN ('food_photo','whoop_screenshot') THEN RAISE EXCEPTION 'unsupported kind %', p_kind; END IF;

  UPDATE ingestion_tokens SET last_used_at = NOW() WHERE token_hash = digest(p_token,'sha256');

  -- auth_user_id is NULL for login-less profiles (Dea/Dev) — that is fine, columns are nullable.
  SELECT auth_user_id INTO v_auth FROM profiles WHERE id = v_profile;

  INSERT INTO wearable_sync_log (user_id, profile_id, provider, sync_type, method, status, source_path, started_at)
  VALUES (v_auth, v_profile,
          CASE WHEN p_kind='whoop_screenshot' THEN 'whoop' ELSE 'food' END,
          'incremental', p_method, 'in_progress', p_source_path, NOW())
  RETURNING id INTO v_log;

  -- AI extraction output -> STAGING ONLY (rule #2). PC reviews/promotes later.
  FOR v_rec IN SELECT * FROM jsonb_array_elements(p_payload) LOOP
    v_in := v_in + 1;
    BEGIN
      IF p_kind='food_photo' THEN
        INSERT INTO stg_food_log_review (profile_id, user_id, sync_log_id, meal_type, description,
               calories, protein_g, carbs_g, fat_g, fiber_g, foods, verdict, confidence, raw_text)
        VALUES (v_profile, v_auth, v_log, v_rec->>'meal_type', v_rec->>'description',
               NULLIF(v_rec->>'calories','')::numeric, NULLIF(v_rec->>'protein_g','')::numeric,
               NULLIF(v_rec->>'carbs_g','')::numeric, NULLIF(v_rec->>'fat_g','')::numeric,
               NULLIF(v_rec->>'fiber_g','')::numeric, COALESCE(v_rec->'foods','[]'::jsonb),
               v_rec->>'verdict', NULLIF(v_rec->>'confidence','')::numeric, v_rec::text);
      ELSE -- whoop_screenshot -> biomarker-style staging of extracted metrics
        INSERT INTO stg_biomarker_review (profile_id, user_id, extracted_name, extracted_value,
               extracted_unit, measured_at, confidence, raw_text)
        VALUES (v_profile, v_auth, v_rec->>'metric', NULLIF(v_rec->>'value','')::numeric,
               v_rec->>'unit', NULLIF(v_rec->>'measured_at','')::timestamptz,
               NULLIF(v_rec->>'confidence','')::numeric, v_rec::text);
      END IF;
      v_ok := v_ok + 1;
    EXCEPTION WHEN OTHERS THEN
      v_fail := v_fail + 1;
      INSERT INTO wearable_sync_errors (sync_log_id, record_ref, error_message, raw)
      VALUES (v_log, COALESCE(v_rec->>'description', v_rec->>'metric'), SQLERRM, v_rec);
    END;
  END LOOP;

  UPDATE wearable_sync_log
     SET status = CASE WHEN v_fail = 0 THEN 'success' ELSE 'failed' END,
         records_in = v_in, records_upserted = v_ok, records_failed = v_fail,
         records_synced = v_ok, completed_at = NOW()
   WHERE id = v_log;
  RETURN v_log;
END $$;
REVOKE ALL ON FUNCTION ingest_health_artifact(TEXT,TEXT,TEXT,TEXT,JSONB) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION ingest_health_artifact(TEXT,TEXT,TEXT,TEXT,JSONB) TO anon, authenticated;

-- =============================================================
-- M. RECREATE daily_health_summary WITH security_invoker + profile_id
--    BLOCKER FIX: a plain CREATE VIEW in PG15 runs as owner (postgres) and
--    BYPASSES underlying-table RLS. Latent at one user; leaks ALL profiles'
--    data the moment Dea/Dev rows exist. security_invoker=true makes the view
--    honour the querying user's RLS. Also exposes profile_id and joins on it.
-- =============================================================
DROP VIEW IF EXISTS daily_health_summary;
CREATE VIEW daily_health_summary
WITH (security_invoker = true) AS
SELECT
    c.profile_id,
    c.user_id,
    c.cycle_start::date AS date,
    c.recovery_score_pct, c.hrv_ms, c.resting_hr_bpm, c.blood_oxygen_pct,
    c.day_strain, c.sleep_performance_pct, c.asleep_duration_min,
    c.deep_sws_min, c.rem_min, c.sleep_efficiency_pct, c.sleep_debt_min,
    (SELECT COUNT(*)                 FROM whoop_workouts w WHERE w.cycle_start = c.cycle_start AND w.profile_id = c.profile_id) AS workout_count,
    (SELECT SUM(w.duration_min)      FROM whoop_workouts w WHERE w.cycle_start = c.cycle_start AND w.profile_id = c.profile_id) AS total_workout_min,
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

-- FIX (review #5): DROP+CREATE VIEW loses the original grant; re-grant.
GRANT SELECT ON daily_health_summary TO authenticated;

-- FIX (review #4): re-assert the auto-sprint trigger bindings idempotently so
-- 002 OWNS them (they exist live, but don't assume — rebind to the new functions).
DROP TRIGGER IF EXISTS whoop_cycles_auto_sprint   ON whoop_cycles;
CREATE TRIGGER whoop_cycles_auto_sprint   BEFORE INSERT ON whoop_cycles   FOR EACH ROW EXECUTE FUNCTION trg_auto_tag_sprint();
DROP TRIGGER IF EXISTS whoop_sleeps_auto_sprint   ON whoop_sleeps;
CREATE TRIGGER whoop_sleeps_auto_sprint   BEFORE INSERT ON whoop_sleeps   FOR EACH ROW EXECUTE FUNCTION trg_auto_tag_sprint();
DROP TRIGGER IF EXISTS whoop_workouts_auto_sprint ON whoop_workouts;
CREATE TRIGGER whoop_workouts_auto_sprint BEFORE INSERT ON whoop_workouts FOR EACH ROW EXECUTE FUNCTION trg_auto_tag_sprint();
DROP TRIGGER IF EXISTS whoop_journal_auto_sprint  ON whoop_journal;
CREATE TRIGGER whoop_journal_auto_sprint  BEFORE INSERT ON whoop_journal  FOR EACH ROW EXECUTE FUNCTION trg_auto_tag_sprint();
DROP TRIGGER IF EXISTS food_logs_auto_sprint      ON food_logs;
CREATE TRIGGER food_logs_auto_sprint      BEFORE INSERT ON food_logs      FOR EACH ROW EXECUTE FUNCTION trg_auto_tag_sprint_food();

COMMIT;
