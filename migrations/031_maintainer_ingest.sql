-- Migration 031: maintainer-ingest SECURITY DEFINER RPCs + media_group_id + settle config
--
-- Adds:
--   1. media_inbox.media_group_id (album clustering)
--   2. system_config seeds (settle_sec, confidence_threshold, fire dedup)
--   3. Three SECURITY DEFINER RPCs callable via PostgREST with a plain user JWT:
--      maintainer_ingest_food / maintainer_ingest_biomarker / maintainer_ingest_supplement
--      Guard: is_maintainer() AND has_profile_access(target_profile) — verified 2026-06-07
--   4. REVOKE EXECUTE FROM PUBLIC; GRANT EXECUTE TO authenticated
--
-- Apply:  python scripts/hs_ops.py apply migrations/031_maintainer_ingest.sql
-- Verify: run the DO $$ ... $$ block below against live DB

-- ── 1. media_inbox: album clustering column ───────────────────────────────────

ALTER TABLE public.media_inbox
  ADD COLUMN IF NOT EXISTS media_group_id text NULL;

COMMENT ON COLUMN public.media_inbox.media_group_id IS
  'Telegram media_group_id shared by photos in the same album burst. '
  'NULL for single photos or text messages. '
  'Rows sharing this value are drained as one cluster (one extraction, one log entry).';

-- ── 2. system_config seeds ────────────────────────────────────────────────────

INSERT INTO public.system_config (key, value, description, category, is_active, updated_at)
VALUES
  ('push.inbox_settle_sec',
   '90',
   'Seconds after creation before drain processes a media_inbox item. '
   'Allows album bursts to fully arrive before extraction.',
   'push', true, now()),
  ('ingest.confidence_threshold',
   '0.7',
   'Minimum LLM confidence (0–1) to write directly to prod tables. '
   'Below this → staging review table.',
   'ingest', true, now()),
  ('routine.last_fire_at',
   '"1970-01-01T00:00:00Z"',
   'ISO-8601 timestamp of last Routine fire from telegram-webhook. '
   'Epoch initial value ensures first message always triggers a fire. '
   'Used with updated_at for atomic compare-and-set dedup.',
   'routine', true, '1970-01-01T00:00:00Z'),
  ('routine.fire_dedup_sec',
   '300',
   'Minimum seconds between Routine fires triggered by telegram-webhook. '
   'Prevents multiple rapid messages from spawning duplicate Routine runs.',
   'routine', true, now())
ON CONFLICT (key) DO NOTHING;

-- ── 3. SECURITY DEFINER RPCs ──────────────────────────────────────────────────
--
-- All three functions:
--   - Run as postgres (SECURITY DEFINER) — bypass RLS; guard IS the only gate
--   - Guard: is_maintainer() AND has_profile_access(p_profile_id)
--     Verified 2026-06-07: PC (auth_user_id 0b0e4093-...) satisfies both for Dea's profile
--     via family_memberships rows. Guard is NOT auth logic — it delegates to existing fns.
--   - Confidence threshold read from system_config to comply with no-hardcode rule
--   - High confidence  → production table (INSERT or upsert per table convention)
--   - Low confidence   → staging review table (status='pending')
--   - Returns jsonb {id: uuid, status: 'inserted'|'staged'}

CREATE OR REPLACE FUNCTION public.maintainer_ingest_food(
  p_profile_id  uuid,
  p_meal_type   text,
  p_description text,
  p_calories    numeric     DEFAULT NULL,
  p_protein_g   numeric     DEFAULT NULL,
  p_carbs_g     numeric     DEFAULT NULL,
  p_fat_g       numeric     DEFAULT NULL,
  p_fiber_g     numeric     DEFAULT NULL,
  p_logged_at   timestamptz DEFAULT now(),
  p_source      text        DEFAULT 'telegram',
  p_notes       text        DEFAULT NULL,
  p_foods       jsonb       DEFAULT NULL,
  p_confidence  numeric     DEFAULT 1.0,
  p_raw_text    text        DEFAULT NULL
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  v_threshold numeric;
  v_row_id    uuid;
  v_status    text;
BEGIN
  IF NOT (is_maintainer() AND has_profile_access(p_profile_id)) THEN
    RAISE EXCEPTION 'unauthorized: requires maintainer with family access to target profile';
  END IF;

  SELECT COALESCE((value::text)::numeric, 0.7)
  INTO v_threshold
  FROM system_config
  WHERE key = 'ingest.confidence_threshold' AND is_active = true
  LIMIT 1;
  IF v_threshold IS NULL THEN v_threshold := 0.7; END IF;

  IF p_confidence >= v_threshold THEN
    -- Plain INSERT — no ON CONFLICT. Telegram entries have no source_log_path so
    -- the partial unique index (uq_food_logs_source_row WHERE source_log_path IS NOT NULL)
    -- does not apply. Idempotency is guaranteed upstream by media_inbox atomic claim.
    INSERT INTO food_logs (
      profile_id, user_id, meal_type, description, calories, protein_g,
      carbs_g, fat_g, fiber_g, source, notes, foods, logged_at, log_date
    ) VALUES (
      p_profile_id, auth.uid(), p_meal_type, p_description, p_calories, p_protein_g,
      p_carbs_g, p_fat_g, p_fiber_g, p_source, p_notes, p_foods,
      p_logged_at, (p_logged_at AT TIME ZONE 'UTC')::date
    )
    RETURNING id INTO v_row_id;
    v_status := 'inserted';
  ELSE
    INSERT INTO stg_food_log_review (
      profile_id, user_id, meal_type, description, calories, protein_g,
      carbs_g, fat_g, fiber_g, foods, confidence, status, raw_text
    ) VALUES (
      p_profile_id, auth.uid(), p_meal_type, p_description, p_calories, p_protein_g,
      p_carbs_g, p_fat_g, p_fiber_g, p_foods, p_confidence, 'pending', p_raw_text
    )
    RETURNING id INTO v_row_id;
    v_status := 'staged';
  END IF;

  RETURN jsonb_build_object('id', v_row_id, 'status', v_status);
END;
$$;

CREATE OR REPLACE FUNCTION public.maintainer_ingest_biomarker(
  p_profile_id           uuid,
  p_metric_definition_id uuid,
  p_value                numeric,
  p_unit                 text        DEFAULT NULL,
  p_measured_at          timestamptz DEFAULT now(),
  p_source               text        DEFAULT 'telegram',
  p_notes                text        DEFAULT NULL,
  p_extracted_name       text        DEFAULT NULL,
  p_confidence           numeric     DEFAULT 1.0,
  p_raw_text             text        DEFAULT NULL
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  v_threshold numeric;
  v_row_id    uuid;
  v_status    text;
BEGIN
  IF NOT (is_maintainer() AND has_profile_access(p_profile_id)) THEN
    RAISE EXCEPTION 'unauthorized: requires maintainer with family access to target profile';
  END IF;

  SELECT COALESCE((value::text)::numeric, 0.7)
  INTO v_threshold
  FROM system_config
  WHERE key = 'ingest.confidence_threshold' AND is_active = true
  LIMIT 1;
  IF v_threshold IS NULL THEN v_threshold := 0.7; END IF;

  IF p_confidence >= v_threshold THEN
    -- Upsert on (profile_id, metric_definition_id, measured_at) — re-measurement updates the value
    INSERT INTO biomarkers (
      profile_id, user_id, metric_definition_id, value, unit, source, notes, measured_at
    ) VALUES (
      p_profile_id, auth.uid(), p_metric_definition_id, p_value, p_unit, p_source, p_notes, p_measured_at
    )
    ON CONFLICT (profile_id, metric_definition_id, measured_at)
    DO UPDATE SET
      value  = EXCLUDED.value,
      unit   = EXCLUDED.unit,
      notes  = EXCLUDED.notes,
      source = EXCLUDED.source
    RETURNING id INTO v_row_id;
    v_status := 'inserted';
  ELSE
    INSERT INTO stg_biomarker_review (
      profile_id, user_id, metric_definition_id, extracted_name,
      extracted_value, extracted_unit, measured_at, confidence, status, raw_text
    ) VALUES (
      p_profile_id, auth.uid(), p_metric_definition_id, p_extracted_name,
      p_value, p_unit, p_measured_at, p_confidence, 'pending', p_raw_text
    )
    RETURNING id INTO v_row_id;
    v_status := 'staged';
  END IF;

  RETURN jsonb_build_object('id', v_row_id, 'status', v_status);
END;
$$;

CREATE OR REPLACE FUNCTION public.maintainer_ingest_supplement(
  p_profile_id     uuid,
  p_supplement_id  uuid,
  p_dose_amount    numeric     DEFAULT NULL,
  p_dose_unit      text        DEFAULT NULL,
  p_taken_at       timestamptz DEFAULT now(),
  p_source         text        DEFAULT 'telegram',
  p_notes          text        DEFAULT NULL,
  p_extracted_name text        DEFAULT NULL,
  p_confidence     numeric     DEFAULT 1.0,
  p_raw_text       text        DEFAULT NULL
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  v_threshold numeric;
  v_row_id    uuid;
  v_status    text;
BEGIN
  IF NOT (is_maintainer() AND has_profile_access(p_profile_id)) THEN
    RAISE EXCEPTION 'unauthorized: requires maintainer with family access to target profile';
  END IF;

  SELECT COALESCE((value::text)::numeric, 0.7)
  INTO v_threshold
  FROM system_config
  WHERE key = 'ingest.confidence_threshold' AND is_active = true
  LIMIT 1;
  IF v_threshold IS NULL THEN v_threshold := 0.7; END IF;

  IF p_confidence >= v_threshold THEN
    -- Upsert on (profile_id, supplement_id, taken_on, source)
    -- taken_on is GENERATED ALWAYS AS ((taken_at AT TIME ZONE 'UTC')::date)
    INSERT INTO supplement_intake_logs (
      profile_id, supplement_id, taken_at, source, dose_amount, dose_unit, notes
    ) VALUES (
      p_profile_id, p_supplement_id, p_taken_at, p_source, p_dose_amount, p_dose_unit, p_notes
    )
    ON CONFLICT (profile_id, supplement_id, taken_on, source)
    DO UPDATE SET
      dose_amount = EXCLUDED.dose_amount,
      dose_unit   = EXCLUDED.dose_unit,
      notes       = EXCLUDED.notes
    RETURNING id INTO v_row_id;
    v_status := 'inserted';
  ELSE
    INSERT INTO stg_supplement_intake_review (
      profile_id, supplement_id, extracted_name, extracted_dose, extracted_unit,
      taken_at, confidence, status, raw_text
    ) VALUES (
      p_profile_id, p_supplement_id, p_extracted_name, p_dose_amount, p_dose_unit,
      p_taken_at, p_confidence, 'pending', p_raw_text
    )
    RETURNING id INTO v_row_id;
    v_status := 'staged';
  END IF;

  RETURN jsonb_build_object('id', v_row_id, 'status', v_status);
END;
$$;

-- ── 4. Grants ─────────────────────────────────────────────────────────────────
--
-- PostgreSQL grants EXECUTE to PUBLIC by default. Revoke before granting to role.
-- The guard inside each function is the authorisation gate; these grants are defence-in-depth.

REVOKE EXECUTE ON FUNCTION public.maintainer_ingest_food(
  uuid, text, text, numeric, numeric, numeric, numeric, numeric,
  timestamptz, text, text, jsonb, numeric, text
) FROM PUBLIC;

REVOKE EXECUTE ON FUNCTION public.maintainer_ingest_biomarker(
  uuid, uuid, numeric, text, timestamptz, text, text, text, numeric, text
) FROM PUBLIC;

REVOKE EXECUTE ON FUNCTION public.maintainer_ingest_supplement(
  uuid, uuid, numeric, text, timestamptz, text, text, text, numeric, text
) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION public.maintainer_ingest_food(
  uuid, text, text, numeric, numeric, numeric, numeric, numeric,
  timestamptz, text, text, jsonb, numeric, text
) TO authenticated;

GRANT EXECUTE ON FUNCTION public.maintainer_ingest_biomarker(
  uuid, uuid, numeric, text, timestamptz, text, text, text, numeric, text
) TO authenticated;

GRANT EXECUTE ON FUNCTION public.maintainer_ingest_supplement(
  uuid, uuid, numeric, text, timestamptz, text, text, text, numeric, text
) TO authenticated;

-- ── 5. Verify ─────────────────────────────────────────────────────────────────

DO $$
DECLARE
  v_col_exists boolean;
  v_fn_count   int;
  v_cfg_count  int;
BEGIN
  SELECT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = 'media_inbox'
      AND column_name = 'media_group_id'
  ) INTO v_col_exists;
  ASSERT v_col_exists, 'media_inbox.media_group_id column missing';

  SELECT COUNT(*) INTO v_fn_count
  FROM pg_proc p
  JOIN pg_namespace n ON n.oid = p.pronamespace
  WHERE n.nspname = 'public'
    AND p.proname IN (
      'maintainer_ingest_food',
      'maintainer_ingest_biomarker',
      'maintainer_ingest_supplement'
    );
  ASSERT v_fn_count = 3, 'Expected 3 SECURITY DEFINER RPCs, found ' || v_fn_count;

  SELECT COUNT(*) INTO v_cfg_count
  FROM system_config
  WHERE key IN (
    'push.inbox_settle_sec',
    'ingest.confidence_threshold',
    'routine.last_fire_at',
    'routine.fire_dedup_sec'
  );
  ASSERT v_cfg_count = 4, 'Expected 4 system_config rows, found ' || v_cfg_count;

  RAISE NOTICE 'Migration 031 verify: OK (media_group_id ✓, 3 RPCs ✓, 4 config rows ✓)';
END $$;
