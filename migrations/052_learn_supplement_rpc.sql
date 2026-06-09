-- =============================================================
-- 052_learn_supplement_rpc.sql
-- learn_supplement(p_profile_id, p_name): auto-add a clarified-but-unknown supplement to
-- the catalog (learn-on-clarify). DEDUPS against existing rows by NORMALIZED name (lower +
-- non-alphanumerics→space) so "electrolytes"/"Electrolytes" can't become two rows; stamps
-- source='learned', verified=false, is_custom=true, owner_profile_id so the maintainer can
-- review/prune. Returns the supplement_id (existing on dedup, or the new one).
--
-- SECURITY DEFINER (the drainer can't INSERT the curated catalog directly), gated to
-- maintainer + family access — same contract as maintainer_ingest_*. The drain only calls
-- this for NON-minors; a minor's new item routes to PC instead (no auto-add).
-- =============================================================
BEGIN;

CREATE OR REPLACE FUNCTION public.learn_supplement(p_profile_id uuid, p_name text)
RETURNS uuid
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  v_norm text;
  v_id   uuid;
BEGIN
  IF NOT (is_maintainer() AND has_profile_access(p_profile_id)) THEN
    RAISE EXCEPTION 'unauthorized: requires maintainer with family access';
  END IF;
  IF coalesce(btrim(p_name), '') = '' THEN
    RETURN NULL;
  END IF;

  -- lower() BEFORE the regexp — [^a-z0-9] is case-sensitive, so uppercase must be folded first.
  v_norm := btrim(regexp_replace(lower(p_name), '[^a-z0-9]+', ' ', 'g'));
  IF v_norm = '' THEN
    RETURN NULL;
  END IF;

  -- Dedup: reuse any active row whose normalized display_name OR name matches.
  SELECT id INTO v_id
    FROM supplements
   WHERE is_active
     AND (btrim(regexp_replace(lower(coalesce(display_name, '')), '[^a-z0-9]+', ' ', 'g')) = v_norm
       OR btrim(regexp_replace(lower(coalesce(name, '')),         '[^a-z0-9]+', ' ', 'g')) = v_norm)
   LIMIT 1;
  IF v_id IS NOT NULL THEN
    RETURN v_id;
  END IF;

  INSERT INTO supplements
    (name, display_name, category, default_unit, is_active, is_custom,
     owner_profile_id, source, verified, notes)
  VALUES
    (left(replace(v_norm, ' ', '_'), 60), btrim(p_name), 'supplement', 'unit', true, true,
     p_profile_id, 'learned', false, 'auto-learned via clarify loop')
  RETURNING id INTO v_id;

  RETURN v_id;
END;
$$;

GRANT EXECUTE ON FUNCTION public.learn_supplement(uuid, text) TO authenticated;

COMMIT;
