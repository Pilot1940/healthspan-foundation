-- =============================================================
-- 001_initial_schema.sql
-- Healthspan Foundation — all 36 tables + RLS + policies
-- =============================================================

-- Enable required extensions
CREATE EXTENSION IF NOT EXISTS "pgcrypto";
CREATE EXTENSION IF NOT EXISTS "pg_trgm";

-- =============================================================
-- HELPER: reusable function for updated_at trigger
-- =============================================================
CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
  NEW.updated_at = NOW();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- =============================================================
-- 1. loinc_reference (reference data — no user_id)
-- =============================================================
CREATE TABLE loinc_reference (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  loinc_code  TEXT NOT NULL UNIQUE,
  component   TEXT NOT NULL,
  property    TEXT,
  time_aspect TEXT,
  system      TEXT,
  scale_type  TEXT,
  method_type TEXT,
  long_name   TEXT,
  short_name  TEXT,
  unit        TEXT,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE loinc_reference ENABLE ROW LEVEL SECURITY;

-- Reference data: readable by all authenticated users
CREATE POLICY "loinc_reference_read" ON loinc_reference
  FOR SELECT TO authenticated USING (true);

-- =============================================================
-- 2. metric_definitions (reference data — no user_id)
-- =============================================================
CREATE TABLE metric_definitions (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name            TEXT NOT NULL UNIQUE,
  display_name    TEXT NOT NULL,
  category        TEXT NOT NULL,
  data_type       TEXT NOT NULL DEFAULT 'numeric',
  unit            TEXT,
  loinc_id        UUID REFERENCES loinc_reference(id),
  description     TEXT,
  min_value       NUMERIC,
  max_value       NUMERIC,
  decimal_places  INT DEFAULT 2,
  is_active       BOOLEAN NOT NULL DEFAULT true,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE metric_definitions ENABLE ROW LEVEL SECURITY;

CREATE POLICY "metric_definitions_read" ON metric_definitions
  FOR SELECT TO authenticated USING (true);

CREATE TRIGGER trg_metric_definitions_updated_at
  BEFORE UPDATE ON metric_definitions
  FOR EACH ROW EXECUTE FUNCTION update_updated_at();

-- =============================================================
-- 3. canonical_aliases
-- =============================================================
CREATE TABLE canonical_aliases (
  id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  alias               TEXT NOT NULL,
  metric_definition_id UUID NOT NULL REFERENCES metric_definitions(id),
  source              TEXT,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (alias, source)
);

ALTER TABLE canonical_aliases ENABLE ROW LEVEL SECURITY;

CREATE POLICY "canonical_aliases_read" ON canonical_aliases
  FOR SELECT TO authenticated USING (true);

-- =============================================================
-- 4. users_extended
-- =============================================================
CREATE TABLE users_extended (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id         UUID NOT NULL UNIQUE REFERENCES auth.users(id) ON DELETE CASCADE,
  display_name    TEXT,
  date_of_birth   DATE,
  sex             TEXT CHECK (sex IN ('male', 'female', 'other')),
  blood_type      TEXT,
  tier            TEXT NOT NULL DEFAULT 'free' CHECK (tier IN ('free', 'pro', 'premium')),
  timezone        TEXT NOT NULL DEFAULT 'UTC',
  unit_system     TEXT NOT NULL DEFAULT 'metric' CHECK (unit_system IN ('metric', 'imperial')),
  onboarding_complete BOOLEAN NOT NULL DEFAULT false,
  avatar_url      TEXT,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE users_extended ENABLE ROW LEVEL SECURITY;

CREATE POLICY "users_extended_isolation" ON users_extended
  FOR ALL TO authenticated
  USING (user_id = auth.uid())
  WITH CHECK (user_id = auth.uid());

CREATE TRIGGER trg_users_extended_updated_at
  BEFORE UPDATE ON users_extended
  FOR EACH ROW EXECUTE FUNCTION update_updated_at();

-- =============================================================
-- 5. user_invites
-- =============================================================
CREATE TABLE user_invites (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id         UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  invited_email   TEXT NOT NULL,
  status          TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'accepted', 'expired', 'revoked')),
  invited_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  accepted_at     TIMESTAMPTZ,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE user_invites ENABLE ROW LEVEL SECURITY;

CREATE POLICY "user_invites_isolation" ON user_invites
  FOR ALL TO authenticated
  USING (user_id = auth.uid())
  WITH CHECK (user_id = auth.uid());

-- =============================================================
-- 6. user_invite_tokens
-- =============================================================
CREATE TABLE user_invite_tokens (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  invite_id   UUID NOT NULL REFERENCES user_invites(id) ON DELETE CASCADE,
  user_id     UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  token       TEXT NOT NULL UNIQUE,
  expires_at  TIMESTAMPTZ NOT NULL,
  used_at     TIMESTAMPTZ,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE user_invite_tokens ENABLE ROW LEVEL SECURITY;

CREATE POLICY "user_invite_tokens_isolation" ON user_invite_tokens
  FOR ALL TO authenticated
  USING (user_id = auth.uid())
  WITH CHECK (user_id = auth.uid());

-- =============================================================
-- 7. locations (reference — no user_id)
-- =============================================================
CREATE TABLE locations (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name        TEXT NOT NULL,
  city        TEXT,
  state       TEXT,
  country     TEXT NOT NULL,
  country_code TEXT,
  latitude    NUMERIC,
  longitude   NUMERIC,
  timezone    TEXT,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (name, country)
);

ALTER TABLE locations ENABLE ROW LEVEL SECURITY;

CREATE POLICY "locations_read" ON locations
  FOR SELECT TO authenticated USING (true);

-- =============================================================
-- 8. user_locations
-- =============================================================
CREATE TABLE user_locations (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id     UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  location_id UUID NOT NULL REFERENCES locations(id),
  is_current  BOOLEAN NOT NULL DEFAULT false,
  moved_at    DATE,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE user_locations ENABLE ROW LEVEL SECURITY;

CREATE POLICY "user_locations_isolation" ON user_locations
  FOR ALL TO authenticated
  USING (user_id = auth.uid())
  WITH CHECK (user_id = auth.uid());

-- Partial unique: one current location per user
CREATE UNIQUE INDEX idx_user_locations_one_current
  ON user_locations (user_id) WHERE is_current = true;

-- =============================================================
-- 9. user_telegram_links
-- =============================================================
CREATE TABLE user_telegram_links (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id         UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  telegram_chat_id BIGINT NOT NULL,
  telegram_username TEXT,
  is_current      BOOLEAN NOT NULL DEFAULT true,
  verified_at     TIMESTAMPTZ,
  unlinked_at     TIMESTAMPTZ,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE user_telegram_links ENABLE ROW LEVEL SECURITY;

CREATE POLICY "user_telegram_links_isolation" ON user_telegram_links
  FOR ALL TO authenticated
  USING (user_id = auth.uid())
  WITH CHECK (user_id = auth.uid());

-- Partial unique: one current telegram link per user
CREATE UNIQUE INDEX idx_user_telegram_links_one_current
  ON user_telegram_links (user_id) WHERE is_current = true;

-- =============================================================
-- 10. user_tier_history
-- =============================================================
CREATE TABLE user_tier_history (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id     UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  old_tier    TEXT,
  new_tier    TEXT NOT NULL,
  reason      TEXT,
  changed_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE user_tier_history ENABLE ROW LEVEL SECURITY;

CREATE POLICY "user_tier_history_isolation" ON user_tier_history
  FOR ALL TO authenticated
  USING (user_id = auth.uid())
  WITH CHECK (user_id = auth.uid());

-- =============================================================
-- 11. user_preference_history
-- =============================================================
CREATE TABLE user_preference_history (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id     UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  key         TEXT NOT NULL,
  old_value   JSONB,
  new_value   JSONB NOT NULL,
  changed_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE user_preference_history ENABLE ROW LEVEL SECURITY;

CREATE POLICY "user_preference_history_isolation" ON user_preference_history
  FOR ALL TO authenticated
  USING (user_id = auth.uid())
  WITH CHECK (user_id = auth.uid());

-- =============================================================
-- 12. weight_logs
-- =============================================================
CREATE TABLE weight_logs (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id         UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  weight_kg       NUMERIC NOT NULL,
  source          TEXT NOT NULL DEFAULT 'manual',
  logged_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  notes           TEXT,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE weight_logs ENABLE ROW LEVEL SECURITY;

CREATE POLICY "weight_logs_isolation" ON weight_logs
  FOR ALL TO authenticated
  USING (user_id = auth.uid())
  WITH CHECK (user_id = auth.uid());

-- =============================================================
-- 13. source_priority_config
-- =============================================================
CREATE TABLE source_priority_config (
  id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id             UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  metric_definition_id UUID NOT NULL REFERENCES metric_definitions(id),
  source              TEXT NOT NULL,
  priority            INT NOT NULL DEFAULT 0,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (user_id, metric_definition_id, source)
);

ALTER TABLE source_priority_config ENABLE ROW LEVEL SECURITY;

CREATE POLICY "source_priority_config_isolation" ON source_priority_config
  FOR ALL TO authenticated
  USING (user_id = auth.uid())
  WITH CHECK (user_id = auth.uid());

CREATE TRIGGER trg_source_priority_config_updated_at
  BEFORE UPDATE ON source_priority_config
  FOR EACH ROW EXECUTE FUNCTION update_updated_at();

-- =============================================================
-- 14. body_metrics_history
-- =============================================================
CREATE TABLE body_metrics_history (
  id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id             UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  metric_definition_id UUID NOT NULL REFERENCES metric_definitions(id),
  value               NUMERIC NOT NULL,
  unit                TEXT,
  source              TEXT NOT NULL DEFAULT 'manual',
  measured_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  notes               TEXT,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE body_metrics_history ENABLE ROW LEVEL SECURITY;

CREATE POLICY "body_metrics_history_isolation" ON body_metrics_history
  FOR ALL TO authenticated
  USING (user_id = auth.uid())
  WITH CHECK (user_id = auth.uid());

-- =============================================================
-- 15. wearable_sync_log
-- =============================================================
CREATE TABLE wearable_sync_log (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id         UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  provider        TEXT NOT NULL,
  sync_type       TEXT NOT NULL DEFAULT 'full',
  status          TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'in_progress', 'success', 'failed')),
  records_synced  INT DEFAULT 0,
  error_message   TEXT,
  started_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  completed_at    TIMESTAMPTZ,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE wearable_sync_log ENABLE ROW LEVEL SECURITY;

CREATE POLICY "wearable_sync_log_isolation" ON wearable_sync_log
  FOR ALL TO authenticated
  USING (user_id = auth.uid())
  WITH CHECK (user_id = auth.uid());

-- =============================================================
-- 16. user_goals
-- =============================================================
CREATE TABLE user_goals (
  id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id             UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  metric_definition_id UUID REFERENCES metric_definitions(id),
  title               TEXT NOT NULL,
  description         TEXT,
  target_value        NUMERIC,
  target_unit         TEXT,
  target_date         DATE,
  is_active           BOOLEAN NOT NULL DEFAULT true,
  progress_pct        NUMERIC DEFAULT 0,
  ended_at            TIMESTAMPTZ,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE user_goals ENABLE ROW LEVEL SECURITY;

CREATE POLICY "user_goals_isolation" ON user_goals
  FOR ALL TO authenticated
  USING (user_id = auth.uid())
  WITH CHECK (user_id = auth.uid());

-- Partial unique: one active goal per user
CREATE UNIQUE INDEX idx_user_goals_one_active
  ON user_goals (user_id) WHERE is_active = TRUE;

CREATE TRIGGER trg_user_goals_updated_at
  BEFORE UPDATE ON user_goals
  FOR EACH ROW EXECUTE FUNCTION update_updated_at();

-- =============================================================
-- 17. biomarker_targets
-- =============================================================
CREATE TABLE biomarker_targets (
  id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id             UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  metric_definition_id UUID NOT NULL REFERENCES metric_definitions(id),
  low_optimal         NUMERIC,
  high_optimal        NUMERIC,
  low_normal          NUMERIC,
  high_normal         NUMERIC,
  source              TEXT NOT NULL DEFAULT 'system',
  notes               TEXT,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (user_id, metric_definition_id, source)
);

ALTER TABLE biomarker_targets ENABLE ROW LEVEL SECURITY;

CREATE POLICY "biomarker_targets_isolation" ON biomarker_targets
  FOR ALL TO authenticated
  USING (user_id = auth.uid())
  WITH CHECK (user_id = auth.uid());

CREATE TRIGGER trg_biomarker_targets_updated_at
  BEFORE UPDATE ON biomarker_targets
  FOR EACH ROW EXECUTE FUNCTION update_updated_at();

-- =============================================================
-- 18. food_rules
-- =============================================================
CREATE TABLE food_rules (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id     UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  rule_type   TEXT NOT NULL CHECK (rule_type IN ('allergy', 'intolerance', 'preference', 'medical')),
  item        TEXT NOT NULL,
  severity    TEXT CHECK (severity IN ('mild', 'moderate', 'severe', 'life_threatening')),
  notes       TEXT,
  source      TEXT NOT NULL DEFAULT 'manual',
  is_active   BOOLEAN NOT NULL DEFAULT true,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE food_rules ENABLE ROW LEVEL SECURITY;

CREATE POLICY "food_rules_isolation" ON food_rules
  FOR ALL TO authenticated
  USING (user_id = auth.uid())
  WITH CHECK (user_id = auth.uid());

CREATE TRIGGER trg_food_rules_updated_at
  BEFORE UPDATE ON food_rules
  FOR EACH ROW EXECUTE FUNCTION update_updated_at();

-- =============================================================
-- 19. documents
-- =============================================================
CREATE TABLE documents (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id         UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  file_name       TEXT NOT NULL,
  file_type       TEXT NOT NULL,
  file_size_bytes BIGINT,
  storage_path    TEXT NOT NULL,
  mime_type       TEXT,
  category        TEXT CHECK (category IN ('lab_report', 'prescription', 'imaging', 'insurance', 'other')),
  status          TEXT NOT NULL DEFAULT 'uploaded' CHECK (status IN ('uploaded', 'processing', 'processed', 'failed')),
  ai_extracted    BOOLEAN NOT NULL DEFAULT false,
  metadata        JSONB DEFAULT '{}',
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE documents ENABLE ROW LEVEL SECURITY;

-- User isolation
CREATE POLICY "documents_isolation" ON documents
  FOR ALL TO authenticated
  USING (user_id = auth.uid())
  WITH CHECK (user_id = auth.uid());

-- Admin shadow read (service role can read all documents)
CREATE POLICY "documents_admin_shadow_read" ON documents
  FOR SELECT TO service_role
  USING (true);

CREATE TRIGGER trg_documents_updated_at
  BEFORE UPDATE ON documents
  FOR EACH ROW EXECUTE FUNCTION update_updated_at();

-- =============================================================
-- 20. stg_biomarker_review (AI staging)
-- =============================================================
CREATE TABLE stg_biomarker_review (
  id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id             UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  document_id         UUID REFERENCES documents(id) ON DELETE SET NULL,
  metric_definition_id UUID REFERENCES metric_definitions(id),
  extracted_name      TEXT NOT NULL,
  extracted_value     NUMERIC,
  extracted_unit      TEXT,
  measured_at         TIMESTAMPTZ,
  confidence          NUMERIC CHECK (confidence >= 0 AND confidence <= 1),
  status              TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected', 'merged')),
  reviewed_at         TIMESTAMPTZ,
  raw_text            TEXT,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE stg_biomarker_review ENABLE ROW LEVEL SECURITY;

CREATE POLICY "stg_biomarker_review_isolation" ON stg_biomarker_review
  FOR ALL TO authenticated
  USING (user_id = auth.uid())
  WITH CHECK (user_id = auth.uid());

-- =============================================================
-- 21. stg_food_rule_review (AI staging)
-- =============================================================
CREATE TABLE stg_food_rule_review (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id     UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  document_id UUID REFERENCES documents(id) ON DELETE SET NULL,
  rule_type   TEXT,
  item        TEXT NOT NULL,
  severity    TEXT,
  confidence  NUMERIC CHECK (confidence >= 0 AND confidence <= 1),
  status      TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected', 'merged')),
  reviewed_at TIMESTAMPTZ,
  raw_text    TEXT,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE stg_food_rule_review ENABLE ROW LEVEL SECURITY;

CREATE POLICY "stg_food_rule_review_isolation" ON stg_food_rule_review
  FOR ALL TO authenticated
  USING (user_id = auth.uid())
  WITH CHECK (user_id = auth.uid());

-- =============================================================
-- 22. stg_test_result_review (AI staging)
-- =============================================================
CREATE TABLE stg_test_result_review (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id         UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  document_id     UUID REFERENCES documents(id) ON DELETE SET NULL,
  test_name       TEXT NOT NULL,
  result_value    TEXT,
  result_unit     TEXT,
  tested_at       TIMESTAMPTZ,
  lab_name        TEXT,
  confidence      NUMERIC CHECK (confidence >= 0 AND confidence <= 1),
  status          TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected', 'merged')),
  reviewed_at     TIMESTAMPTZ,
  raw_text        TEXT,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE stg_test_result_review ENABLE ROW LEVEL SECURITY;

CREATE POLICY "stg_test_result_review_isolation" ON stg_test_result_review
  FOR ALL TO authenticated
  USING (user_id = auth.uid())
  WITH CHECK (user_id = auth.uid());

-- =============================================================
-- 23. biomarkers
-- =============================================================
CREATE TABLE biomarkers (
  id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id             UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  metric_definition_id UUID NOT NULL REFERENCES metric_definitions(id),
  value               NUMERIC NOT NULL,
  unit                TEXT,
  source              TEXT NOT NULL DEFAULT 'manual',
  document_id         UUID REFERENCES documents(id) ON DELETE SET NULL,
  staging_id          UUID REFERENCES stg_biomarker_review(id) ON DELETE SET NULL,
  measured_at         TIMESTAMPTZ NOT NULL,
  lab_name            TEXT,
  notes               TEXT,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE biomarkers ENABLE ROW LEVEL SECURITY;

CREATE POLICY "biomarkers_isolation" ON biomarkers
  FOR ALL TO authenticated
  USING (user_id = auth.uid())
  WITH CHECK (user_id = auth.uid());

-- =============================================================
-- 24. daily_logs
-- =============================================================
CREATE TABLE daily_logs (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id     UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  log_date    DATE NOT NULL,
  log_type    TEXT NOT NULL DEFAULT 'general',
  source      TEXT NOT NULL DEFAULT 'manual',
  notes       TEXT,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (user_id, log_date, log_type)
);

ALTER TABLE daily_logs ENABLE ROW LEVEL SECURITY;

CREATE POLICY "daily_logs_isolation" ON daily_logs
  FOR ALL TO authenticated
  USING (user_id = auth.uid())
  WITH CHECK (user_id = auth.uid());

CREATE TRIGGER trg_daily_logs_updated_at
  BEFORE UPDATE ON daily_logs
  FOR EACH ROW EXECUTE FUNCTION update_updated_at();

-- =============================================================
-- 25. daily_log_metrics
-- =============================================================
CREATE TABLE daily_log_metrics (
  id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id             UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  daily_log_id        UUID NOT NULL REFERENCES daily_logs(id) ON DELETE CASCADE,
  metric_definition_id UUID NOT NULL REFERENCES metric_definitions(id),
  value               NUMERIC NOT NULL,
  unit                TEXT,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE daily_log_metrics ENABLE ROW LEVEL SECURITY;

CREATE POLICY "daily_log_metrics_isolation" ON daily_log_metrics
  FOR ALL TO authenticated
  USING (user_id = auth.uid())
  WITH CHECK (user_id = auth.uid());

-- =============================================================
-- 26. food_logs
-- =============================================================
CREATE TABLE food_logs (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id     UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  meal_type   TEXT CHECK (meal_type IN ('breakfast', 'lunch', 'dinner', 'snack', 'drink', 'supplement')),
  description TEXT NOT NULL,
  calories    NUMERIC,
  protein_g   NUMERIC,
  carbs_g     NUMERIC,
  fat_g       NUMERIC,
  fiber_g     NUMERIC,
  source      TEXT NOT NULL DEFAULT 'manual',
  logged_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  image_url   TEXT,
  notes       TEXT,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE food_logs ENABLE ROW LEVEL SECURITY;

CREATE POLICY "food_logs_isolation" ON food_logs
  FOR ALL TO authenticated
  USING (user_id = auth.uid())
  WITH CHECK (user_id = auth.uid());

-- =============================================================
-- 27. test_definitions (reference data — no user_id)
-- =============================================================
CREATE TABLE test_definitions (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name            TEXT NOT NULL UNIQUE,
  display_name    TEXT NOT NULL,
  category        TEXT NOT NULL,
  description     TEXT,
  biomarker_count INT DEFAULT 0,
  is_active       BOOLEAN NOT NULL DEFAULT true,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE test_definitions ENABLE ROW LEVEL SECURITY;

CREATE POLICY "test_definitions_read" ON test_definitions
  FOR SELECT TO authenticated USING (true);

CREATE TRIGGER trg_test_definitions_updated_at
  BEFORE UPDATE ON test_definitions
  FOR EACH ROW EXECUTE FUNCTION update_updated_at();

-- =============================================================
-- 28. test_targets
-- =============================================================
CREATE TABLE test_targets (
  id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  test_definition_id  UUID NOT NULL REFERENCES test_definitions(id),
  metric_definition_id UUID NOT NULL REFERENCES metric_definitions(id),
  low_normal          NUMERIC,
  high_normal         NUMERIC,
  low_optimal         NUMERIC,
  high_optimal        NUMERIC,
  age_min             INT,
  age_max             INT,
  sex                 TEXT CHECK (sex IN ('male', 'female', 'all')),
  created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE test_targets ENABLE ROW LEVEL SECURITY;

CREATE POLICY "test_targets_read" ON test_targets
  FOR SELECT TO authenticated USING (true);

CREATE TRIGGER trg_test_targets_updated_at
  BEFORE UPDATE ON test_targets
  FOR EACH ROW EXECUTE FUNCTION update_updated_at();

-- =============================================================
-- 29. healthspan_tests
-- =============================================================
CREATE TABLE healthspan_tests (
  id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id             UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  test_definition_id  UUID NOT NULL REFERENCES test_definitions(id),
  document_id         UUID REFERENCES documents(id) ON DELETE SET NULL,
  tested_at           TIMESTAMPTZ NOT NULL,
  lab_name            TEXT,
  status              TEXT NOT NULL DEFAULT 'complete' CHECK (status IN ('pending', 'complete', 'partial')),
  notes               TEXT,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE healthspan_tests ENABLE ROW LEVEL SECURITY;

CREATE POLICY "healthspan_tests_isolation" ON healthspan_tests
  FOR ALL TO authenticated
  USING (user_id = auth.uid())
  WITH CHECK (user_id = auth.uid());

CREATE TRIGGER trg_healthspan_tests_updated_at
  BEFORE UPDATE ON healthspan_tests
  FOR EACH ROW EXECUTE FUNCTION update_updated_at();

-- =============================================================
-- 30. system_config (reference data — no user_id)
-- =============================================================
CREATE TABLE system_config (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  key         TEXT NOT NULL UNIQUE,
  value       JSONB NOT NULL,
  description TEXT,
  category    TEXT,
  is_active   BOOLEAN NOT NULL DEFAULT true,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE system_config ENABLE ROW LEVEL SECURITY;

CREATE POLICY "system_config_read" ON system_config
  FOR SELECT TO authenticated USING (true);

CREATE TRIGGER trg_system_config_updated_at
  BEFORE UPDATE ON system_config
  FOR EACH ROW EXECUTE FUNCTION update_updated_at();

-- =============================================================
-- 31. brain_conversations
-- =============================================================
CREATE TABLE brain_conversations (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id     UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  title       TEXT,
  channel     TEXT NOT NULL DEFAULT 'telegram' CHECK (channel IN ('telegram', 'web', 'api')),
  status      TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'archived')),
  metadata    JSONB DEFAULT '{}',
  created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE brain_conversations ENABLE ROW LEVEL SECURITY;

CREATE POLICY "brain_conversations_isolation" ON brain_conversations
  FOR ALL TO authenticated
  USING (user_id = auth.uid())
  WITH CHECK (user_id = auth.uid());

CREATE TRIGGER trg_brain_conversations_updated_at
  BEFORE UPDATE ON brain_conversations
  FOR EACH ROW EXECUTE FUNCTION update_updated_at();

-- =============================================================
-- 32. brain_messages
-- =============================================================
CREATE TABLE brain_messages (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id         UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  conversation_id UUID NOT NULL REFERENCES brain_conversations(id) ON DELETE CASCADE,
  role            TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
  content         TEXT NOT NULL,
  token_count     INT,
  model           TEXT,
  metadata        JSONB DEFAULT '{}',
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE brain_messages ENABLE ROW LEVEL SECURITY;

CREATE POLICY "brain_messages_isolation" ON brain_messages
  FOR ALL TO authenticated
  USING (user_id = auth.uid())
  WITH CHECK (user_id = auth.uid());

-- =============================================================
-- 33. log_type_config (reference data — no user_id)
-- =============================================================
CREATE TABLE log_type_config (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  log_type    TEXT NOT NULL UNIQUE,
  display_name TEXT NOT NULL,
  description TEXT,
  icon        TEXT,
  default_metrics JSONB DEFAULT '[]',
  is_active   BOOLEAN NOT NULL DEFAULT true,
  sort_order  INT NOT NULL DEFAULT 0,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE log_type_config ENABLE ROW LEVEL SECURITY;

CREATE POLICY "log_type_config_read" ON log_type_config
  FOR SELECT TO authenticated USING (true);

CREATE TRIGGER trg_log_type_config_updated_at
  BEFORE UPDATE ON log_type_config
  FOR EACH ROW EXECUTE FUNCTION update_updated_at();

-- =============================================================
-- 34. user_log_type_prefs
-- =============================================================
CREATE TABLE user_log_type_prefs (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id         UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  log_type_config_id UUID NOT NULL REFERENCES log_type_config(id),
  is_enabled      BOOLEAN NOT NULL DEFAULT true,
  sort_order      INT,
  custom_metrics  JSONB DEFAULT '[]',
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (user_id, log_type_config_id)
);

ALTER TABLE user_log_type_prefs ENABLE ROW LEVEL SECURITY;

CREATE POLICY "user_log_type_prefs_isolation" ON user_log_type_prefs
  FOR ALL TO authenticated
  USING (user_id = auth.uid())
  WITH CHECK (user_id = auth.uid());

CREATE TRIGGER trg_user_log_type_prefs_updated_at
  BEFORE UPDATE ON user_log_type_prefs
  FOR EACH ROW EXECUTE FUNCTION update_updated_at();

-- =============================================================
-- 35. trend_alerts
-- =============================================================
CREATE TABLE trend_alerts (
  id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id             UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  metric_definition_id UUID REFERENCES metric_definitions(id),
  alert_type          TEXT NOT NULL CHECK (alert_type IN ('threshold', 'trend', 'anomaly', 'goal')),
  severity            TEXT NOT NULL DEFAULT 'info' CHECK (severity IN ('info', 'warning', 'critical')),
  title               TEXT NOT NULL,
  message             TEXT NOT NULL,
  data                JSONB DEFAULT '{}',
  is_read             BOOLEAN NOT NULL DEFAULT false,
  read_at             TIMESTAMPTZ,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE trend_alerts ENABLE ROW LEVEL SECURITY;

CREATE POLICY "trend_alerts_isolation" ON trend_alerts
  FOR ALL TO authenticated
  USING (user_id = auth.uid())
  WITH CHECK (user_id = auth.uid());

-- =============================================================
-- 36. audit_log
-- =============================================================
CREATE TABLE audit_log (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id     UUID REFERENCES auth.users(id) ON DELETE SET NULL,
  action      TEXT NOT NULL,
  table_name  TEXT NOT NULL,
  record_id   UUID,
  old_data    JSONB,
  new_data    JSONB,
  ip_address  INET,
  user_agent  TEXT,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE audit_log ENABLE ROW LEVEL SECURITY;

CREATE POLICY "audit_log_isolation" ON audit_log
  FOR ALL TO authenticated
  USING (user_id = auth.uid())
  WITH CHECK (user_id = auth.uid());

-- =============================================================
-- PERFORMANCE INDEXES
-- =============================================================
CREATE INDEX idx_biomarkers_user_metric ON biomarkers (user_id, metric_definition_id, measured_at DESC);
CREATE INDEX idx_biomarkers_measured_at ON biomarkers (measured_at DESC);
CREATE INDEX idx_daily_logs_user_date ON daily_logs (user_id, log_date DESC);
CREATE INDEX idx_daily_log_metrics_log ON daily_log_metrics (daily_log_id);
CREATE INDEX idx_food_logs_user_date ON food_logs (user_id, logged_at DESC);
CREATE INDEX idx_weight_logs_user_date ON weight_logs (user_id, logged_at DESC);
CREATE INDEX idx_brain_messages_conversation ON brain_messages (conversation_id, created_at);
CREATE INDEX idx_brain_conversations_user ON brain_conversations (user_id, updated_at DESC);
CREATE INDEX idx_documents_user ON documents (user_id, created_at DESC);
CREATE INDEX idx_healthspan_tests_user ON healthspan_tests (user_id, tested_at DESC);
CREATE INDEX idx_trend_alerts_user_unread ON trend_alerts (user_id, is_read, created_at DESC);
CREATE INDEX idx_audit_log_user ON audit_log (user_id, created_at DESC);
CREATE INDEX idx_audit_log_table ON audit_log (table_name, created_at DESC);
CREATE INDEX idx_stg_biomarker_review_user ON stg_biomarker_review (user_id, status);
CREATE INDEX idx_stg_food_rule_review_user ON stg_food_rule_review (user_id, status);
CREATE INDEX idx_stg_test_result_review_user ON stg_test_result_review (user_id, status);
CREATE INDEX idx_body_metrics_history_user ON body_metrics_history (user_id, metric_definition_id, measured_at DESC);
CREATE INDEX idx_wearable_sync_log_user ON wearable_sync_log (user_id, started_at DESC);
CREATE INDEX idx_canonical_aliases_metric ON canonical_aliases (metric_definition_id);
