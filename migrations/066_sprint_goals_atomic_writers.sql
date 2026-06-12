-- =============================================================
-- 066_sprint_goals_atomic_writers.sql
-- Lost-update fix for sprints.goals. TWO surfaces write the SAME goals jsonb on the
-- SAME row: the TRACKING surface (Telegram adherence ticks → goals.adherence_log) and
-- the PLANNING surface (claude.ai skill → goals.daily_overrides). Both previously did a
-- full-object read-modify-write, so a tick landing between the planner's read and write
-- (or vice-versa) was silently reverted — this dropped PC's 'beach' tick on 2026-06-12.
--
-- The durable fix: field-scoped, atomic writers. Each merges ONLY its own subtree via a
-- single jsonb_set UPDATE, which operates on the CURRENT row value server-side — so a
-- concurrent write to the OTHER subtree cannot be clobbered, and two writes to the SAME
-- subtree serialize on the row lock (the loser re-reads the winner's value). lib/sprints
-- (psycopg2 path) already does this; these RPCs give the Telegram (service_role) and REST
-- paths the same guarantee, and become the single write contract for both surfaces.
--
-- SECURITY: SECURITY DEFINER (so the merge runs regardless of the caller's table grants),
-- gated like the rest of the codebase:
--   * authenticated callers (the skill — claims carry role='authenticated') must hold
--     has_profile_access(p_profile_id);
--   * service_role callers (the telegram-webhook, no JWT profile claim) are trusted but
--     STILL pinned to the passed profile via the sprint-ownership check below — defense in
--     depth, mirroring the webhook's own `sprint.profile_id === profileId` guard.
-- The sprint must belong to p_profile_id in BOTH paths; this is what stops a logged-in
-- user (or a mis-passed id) from ticking another profile's sprint.
-- =============================================================
BEGIN;

-- Allowed adherence activities — keep in lockstep with lib/sprints.ACTIVITIES and the
-- TICK_ACTIVITIES list in supabase/functions/telegram-webhook/index.ts.
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
  IF p_activity NOT IN ('gym','beach','pool','hike','massage') THEN
    RAISE EXCEPTION 'invalid activity %, expected gym|beach|pool|hike|massage', p_activity;
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

CREATE OR REPLACE FUNCTION public.sprint_set_override(
  p_sprint_id  uuid,
  p_date       text,
  p_override   jsonb,
  p_profile_id uuid
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  v_out jsonb;
BEGIN
  IF p_date !~ '^\d{4}-\d{2}-\d{2}$' THEN
    RAISE EXCEPTION 'invalid date %, expected YYYY-MM-DD', p_date;
  END IF;
  IF p_override IS NOT NULL AND jsonb_typeof(p_override) <> 'object' THEN
    RAISE EXCEPTION 'override must be a json object or null, got %', jsonb_typeof(p_override);
  END IF;

  IF auth.role() IS DISTINCT FROM 'service_role' AND NOT has_profile_access(p_profile_id) THEN
    RAISE EXCEPTION 'unauthorized: no access to profile %', p_profile_id;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM sprints WHERE id = p_sprint_id AND profile_id = p_profile_id) THEN
    RAISE EXCEPTION 'sprint % not owned by profile %', p_sprint_id, p_profile_id;
  END IF;

  IF p_override IS NULL OR p_override = '{}'::jsonb THEN
    -- CLEAR the date's override (minus-key) without touching adherence_log
    UPDATE public.sprints
       SET goals = jsonb_set(COALESCE(goals, '{}'::jsonb), '{daily_overrides}',
                     COALESCE(goals #> '{daily_overrides}', '{}'::jsonb) - p_date, true)
     WHERE id = p_sprint_id;
    v_out := NULL;
  ELSE
    UPDATE public.sprints
       SET goals = jsonb_set(COALESCE(goals, '{}'::jsonb),
                     ARRAY['daily_overrides', p_date], p_override, true)
     WHERE id = p_sprint_id
     RETURNING goals #> ARRAY['daily_overrides', p_date] INTO v_out;
  END IF;

  RETURN v_out;
END;
$$;

-- authenticated = the planning skill; service_role = the telegram-webhook tracking surface.
GRANT EXECUTE ON FUNCTION public.sprint_set_adherence(uuid, text, text, boolean, uuid)
  TO authenticated, service_role;
GRANT EXECUTE ON FUNCTION public.sprint_set_override(uuid, text, jsonb, uuid)
  TO authenticated, service_role;

COMMENT ON FUNCTION public.sprint_set_adherence(uuid, text, text, boolean, uuid) IS
  'Atomic, subtree-scoped write of goals.adherence_log[date][activity]. Ownership-gated. '
  'Single write contract for Telegram ticks (service_role) + lib/sprints.mark_done. mig 066.';
COMMENT ON FUNCTION public.sprint_set_override(uuid, text, jsonb, uuid) IS
  'Atomic, subtree-scoped write of goals.daily_overrides[date] (NULL/{} clears). Ownership-gated. '
  'Single write contract for the claude.ai planning skill + lib/sprints.set_override. mig 066.';

COMMIT;
