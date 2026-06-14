-- =============================================================
-- 068_sprint_adherence_nutrition_keys.sql
-- Allow FOOD micronutrient check-in keys in the sprint adherence store.
--
-- Dea (minor, App/supabase_client bundle) tracks IRON / CALCIUM / VITAMIN D via FOOD —
-- no pills, no supplement_regimens (pill-semantic + maintainer-locked catalog + supplement
-- analytics don't run over REST). We reuse the sprint adherence_log (already REST-read/written
-- via sprint_set_adherence and rendered in the brief) to store food check-in ticks alongside
-- the workout activities.
--
-- mig 066's sprint_set_adherence hard-validated p_activity against the 5 workout activities,
-- so a REST `.rpc('iron',…)` was rejected server-side. This migration CREATE OR REPLACEs the
-- function to additionally accept 'iron','calcium','vitamin_d'. Everything else (ownership
-- gate, atomic subtree jsonb_set, return shape) is byte-for-byte identical to mig 066.
--
-- Function-definition change ONLY — no base-table data touched. The nutrition keys live in the
-- SAME goals.adherence_log[date] object as the workout keys; the brief renders them on a
-- separate "🥗 Food check-in" line (lib/sprints), so the two never collide.
-- =============================================================

CREATE OR REPLACE FUNCTION public.sprint_set_adherence(
  p_sprint_id  uuid,
  p_date       text,
  p_activity   text,
  p_value      boolean,
  p_profile_id uuid
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  v_day jsonb;
BEGIN
  -- workout activities (mig 066) + food micronutrient check-in keys (mig 068)
  IF p_activity NOT IN ('gym','beach','pool','hike','massage',
                        'iron','calcium','vitamin_d') THEN
    RAISE EXCEPTION 'invalid activity %, expected gym|beach|pool|hike|massage|iron|calcium|vitamin_d', p_activity;
  END IF;
  IF p_date !~ '^\d{4}-\d{2}-\d{2}$' THEN
    RAISE EXCEPTION 'invalid date %, expected YYYY-MM-DD', p_date;
  END IF;

  -- authenticated path must have family access; service_role is trusted (ownership pinned below)
  IF auth.role() IS DISTINCT FROM 'service_role' AND NOT has_profile_access(p_profile_id) THEN
    RAISE EXCEPTION 'unauthorized: no access to profile %', p_profile_id;
  END IF;
  -- the sprint must belong to the passed profile (both paths)
  IF NOT EXISTS (SELECT 1 FROM sprints WHERE id = p_sprint_id AND profile_id = p_profile_id) THEN
    RAISE EXCEPTION 'sprint % not owned by profile %', p_sprint_id, p_profile_id;
  END IF;

  -- Atomic, subtree-scoped merge: sets goals.adherence_log[date][activity] only. Reads the
  -- current row value, so a concurrent daily_overrides write is untouched. create_missing=true
  -- builds adherence_log / the day object if absent.
  UPDATE public.sprints
     SET goals = jsonb_set(
           jsonb_set(COALESCE(goals, '{}'::jsonb), '{adherence_log}',
                     COALESCE(goals #> '{adherence_log}', '{}'::jsonb), true),
           ARRAY['adherence_log', p_date],
           COALESCE(goals #> ARRAY['adherence_log', p_date], '{}'::jsonb)
             || jsonb_build_object(p_activity, p_value),
           true)
   WHERE id = p_sprint_id
   RETURNING goals #> ARRAY['adherence_log', p_date] INTO v_day;

  RETURN COALESCE(v_day, '{}'::jsonb);
END;
$$;

COMMENT ON FUNCTION public.sprint_set_adherence(uuid, text, text, boolean, uuid) IS
  'Atomic, subtree-scoped write of goals.adherence_log[date][activity]. Ownership-gated. '
  'Activities: workout (gym|beach|pool|hike|massage) + food check-in (iron|calcium|vitamin_d, '
  'mig 068). Single write contract for Telegram ticks (service_role) + lib/sprints.mark_done.';
