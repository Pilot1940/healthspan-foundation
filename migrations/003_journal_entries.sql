-- =============================================================
-- 003_journal_entries.sql  (ADDITIVE — nothing dropped here)
-- HealthSpan: long-format, lossless, extensible WHOOP Journal.
--
-- WHY: the 27 fixed boolean columns on whoop_journal are rigid & lossy —
--   * WHOOP supports 300+ behaviours, user-selectable & custom (fixed cols can't hold them)
--   * question text DRIFTS ("Took a calcium supplement" vs "...?") -> silent mis-map
--   * the CSV "Notes" field (1,296 populated entries) is dropped entirely
--   * WHOOP "Quantity Fields" (caffeine servings, alcohol drink count) have no home
-- WHAT: a behaviour CATALOG (mirrors metric_definitions + canonical_aliases) and a
--   long-format whoop_journal_entries table (one row per profile×cycle×behaviour) with
--   answered_yes + nullable quantity/unit + notes. whoop_journal stays UNTOUCHED here;
--   the table->pivot-view swap + writer repoint happens in a follow-up migration AFTER
--   backfill + reconcile (advisor gate: backup -> backfill -> reconcile -> then drop).
-- =============================================================
BEGIN;

-- -------------------------------------------------------------
-- 1. journal_behaviours catalog (shared reference, like metric_definitions)
-- -------------------------------------------------------------
CREATE TABLE IF NOT EXISTS journal_behaviors (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  slug          TEXT NOT NULL UNIQUE,                 -- stable key, e.g. 'had_alcohol'
  question_text TEXT NOT NULL,                        -- canonical WHOOP question
  display_name  TEXT NOT NULL,
  category      TEXT NOT NULL CHECK (category IN ('diet','supplements','hydration','alcohol','sleep','wellness','other')),
  is_quantitative BOOLEAN NOT NULL DEFAULT false,     -- true => quantity field meaningful
  default_unit  TEXT,                                 -- e.g. 'drinks','servings'
  is_active     BOOLEAN NOT NULL DEFAULT true,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
ALTER TABLE journal_behaviors ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS journal_behaviors_read ON journal_behaviors;
CREATE POLICY journal_behaviors_read ON journal_behaviors FOR SELECT TO authenticated USING (true);
DROP TRIGGER IF EXISTS trg_journal_behaviors_updated_at ON journal_behaviors;
CREATE TRIGGER trg_journal_behaviors_updated_at BEFORE UPDATE ON journal_behaviors
  FOR EACH ROW EXECUTE FUNCTION update_updated_at();

-- alias table for question-text drift (mirrors canonical_aliases)
CREATE TABLE IF NOT EXISTS journal_behavior_aliases (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  alias       TEXT NOT NULL UNIQUE,                   -- raw question text variant
  behavior_id UUID NOT NULL REFERENCES journal_behaviors(id) ON DELETE CASCADE,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
ALTER TABLE journal_behavior_aliases ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS journal_behavior_aliases_read ON journal_behavior_aliases;
CREATE POLICY journal_behavior_aliases_read ON journal_behavior_aliases FOR SELECT TO authenticated USING (true);

-- -------------------------------------------------------------
-- 2. Seed the 27 known behaviours (slug, question, category, quantitative)
-- -------------------------------------------------------------
INSERT INTO journal_behaviors (slug, question_text, display_name, category, is_quantitative, default_unit) VALUES
  ('gluten_free_diet',        'Following a gluten-free diet?',              'Gluten-free diet',        'diet',        false, NULL),
  ('consumed_meat',           'Consumed meat?',                             'Consumed meat',           'diet',        false, NULL),
  ('ate_during_daylight',     'Ate all your meals during daylight hours?',  'Ate during daylight',     'diet',        false, NULL),
  ('consumed_added_sugar',    'Consumed added sugar?',                      'Consumed added sugar',    'diet',        false, NULL),
  ('consumed_dairy',          'Consumed dairy?',                            'Consumed dairy',          'diet',        false, NULL),
  ('consumed_protein',        'Consumed protein?',                          'Consumed protein',        'diet',        false, NULL),
  ('intermittent_fasting',    'Followed an intermittent fasting diet?',     'Intermittent fasting',    'diet',        false, NULL),
  ('consumed_fruits_veg',     'Consumed fruits and/or vegetables?',         'Fruits/vegetables',       'diet',        false, NULL),
  ('consumed_carbs',          'Consumed carbohydrates?',                    'Consumed carbs',          'diet',        false, NULL),
  ('consumed_caffeine',       'Consumed caffeine?',                         'Consumed caffeine',       'diet',        true,  'servings'),
  ('avoided_processed_foods', 'Avoided processed foods?',                   'Avoided processed foods', 'diet',        false, NULL),
  ('ate_close_to_bedtime',    'Ate food close to bedtime?',                 'Ate close to bedtime',    'diet',        false, NULL),
  ('took_fish_oil',           'Took fish oil?',                             'Fish oil',                'supplements', false, NULL),
  ('took_electrolytes',       'Took electrolyte supplements?',              'Electrolytes',            'supplements', false, NULL),
  ('took_calcium_supplement', 'Took a calcium supplement?',                 'Calcium supplement',      'supplements', false, NULL),
  ('hydrated_sufficiently',   'Hydrated sufficiently?',                     'Hydrated sufficiently',   'hydration',   false, NULL),
  ('had_alcohol',             'Have any alcoholic drinks?',                 'Alcohol',                 'alcohol',     true,  'drinks'),
  ('read_non_screen_in_bed',  'Read (non-screened device) while in bed?',   'Read in bed',             'sleep',       false, NULL),
  ('wore_mouth_tape',         'Wore mouth tape while sleeping?',            'Mouth tape',              'sleep',       false, NULL),
  ('hot_shower_before_bed',   'Took a hot shower before bed?',              'Hot shower before bed',   'sleep',       false, NULL),
  ('took_cold_shower',        'Took a cold shower?',                        'Cold shower',             'wellness',    false, NULL),
  ('saw_sunlight_on_waking',  'Saw direct sunlight upon waking up?',        'Sunlight on waking',      'wellness',    false, NULL),
  ('journaled_thoughts',      'Journaled your thoughts?',                   'Journaled thoughts',      'wellness',    false, NULL),
  ('learned_something',       'Learned something interesting or important?','Learned something',       'wellness',    false, NULL),
  ('practiced_breathwork',    'Practiced breathwork?',                      'Breathwork',              'wellness',    false, NULL),
  ('connected_family_friends','Connected with family and/or friends?',      'Connected w/ family',     'wellness',    false, NULL),
  ('spent_time_outdoors',     'Spend time outdoors?',                       'Time outdoors',           'wellness',    false, NULL)
ON CONFLICT (slug) DO NOTHING;

-- Known drift alias: WHOOP dropped the trailing '?' on this question in earlier exports
INSERT INTO journal_behavior_aliases (alias, behavior_id)
  SELECT 'Took a calcium supplement', id FROM journal_behaviors WHERE slug='took_calcium_supplement'
  ON CONFLICT (alias) DO NOTHING;
-- Seed canonical question_text as its own alias too, so backfill can match either form
INSERT INTO journal_behavior_aliases (alias, behavior_id)
  SELECT question_text, id FROM journal_behaviors
  ON CONFLICT (alias) DO NOTHING;

-- -------------------------------------------------------------
-- 3. whoop_journal_entries — long-format, lossless, profile-scoped
--    sprint tagging is NOT stored per entry (redundant — all of a cycle's
--    entries share one sprint); join through whoop_cycles on (profile_id, cycle_start).
-- -------------------------------------------------------------
CREATE TABLE IF NOT EXISTS whoop_journal_entries (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  profile_id    UUID NOT NULL REFERENCES profiles(id),
  cycle_start   TIMESTAMPTZ NOT NULL,
  cycle_end     TIMESTAMPTZ,
  timezone      TEXT,
  behavior_id   UUID NOT NULL REFERENCES journal_behaviors(id),
  answered_yes  BOOLEAN,                    -- nullable: 'unsure'/unanswered allowed
  quantity      NUMERIC,                    -- WHOOP quantity field (caffeine servings, alcohol drinks)
  unit          TEXT,
  notes         TEXT,                       -- the previously-dropped free-text Notes
  source_file   TEXT,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (profile_id, cycle_start, behavior_id)
);
ALTER TABLE whoop_journal_entries ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS whoop_journal_entries_profile_access ON whoop_journal_entries;
CREATE POLICY whoop_journal_entries_profile_access ON whoop_journal_entries
  FOR ALL TO authenticated
  USING (has_profile_access(profile_id))
  WITH CHECK (has_profile_access(profile_id));

CREATE INDEX IF NOT EXISTS idx_journal_entries_profile_date ON whoop_journal_entries (profile_id, cycle_start DESC);
CREATE INDEX IF NOT EXISTS idx_journal_entries_behavior     ON whoop_journal_entries (behavior_id);

COMMIT;
