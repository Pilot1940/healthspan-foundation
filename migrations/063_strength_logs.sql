-- =============================================================
-- 063_strength_logs.sql
-- New table: strength_logs — resistance-training sets (load / sets / reps / RIR).
--
-- Mirrors the house patterns exactly:
--   * Soft-delete (mig 060): voided_at/void_reason + maintainer_void_strength()
--     SECURITY DEFINER RPC + read-side `WHERE voided_at IS NULL` filters. Rows
--     are append-only; a mis-logged set is voided, never DELETEd.
--   * RLS + grants copied from food_logs: a single FOR ALL policy on
--     has_profile_access(profile_id); authenticated gets SELECT/INSERT/UPDATE
--     (NO DELETE, NO healthspan_app).
--   * performed_on GENERATED column uses AT TIME ZONE 'UTC' (the immutable form
--     copied verbatim from supplement_intake_logs.taken_on — a bare
--     (timestamptz)::date is STABLE and rejected in a stored generated column).
--
-- Seed is INTENTIONALLY NOT here — this schema is transportable/multi-tenant, so
-- baking PC's personal session into a schema migration would replay his data on
-- every instance. Seed via scripts/seed_strength_063.py (one-off) instead.
--
-- Apply: python3 scripts/hs_ops.py apply migrations/063_strength_logs.sql
-- =============================================================
BEGIN;

-- ── 1. table ──

CREATE TABLE IF NOT EXISTS public.strength_logs (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  profile_id      uuid NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  performed_at    timestamptz NOT NULL,
  -- IMMUTABILITY: (timestamptz)::date is STABLE and is REJECTED in a stored
  -- generated column. AT TIME ZONE 'UTC' is the immutable form — copied verbatim
  -- from the live supplement_intake_logs.taken_on definition.
  performed_on    date GENERATED ALWAYS AS ((performed_at AT TIME ZONE 'UTC')::date) STORED,
  exercise        text NOT NULL,
  modality        text,            -- barbell | machine | cable | dumbbell | bodyweight
  load_value      numeric,
  load_unit       text,            -- kg | lb | bodyweight   (NOT 'plates')
  sets            int,
  reps            int,             -- reps per set in this group
  rir             numeric,         -- reps in reserve
  device_specific boolean DEFAULT false,  -- machine/cable loads not free-weight comparable
  notes           text,
  source          text DEFAULT 'skill',
  voided_at       timestamptz,     -- soft delete (mig 060 pattern); set via RPC, never DELETE
  void_reason     text,
  created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS strength_logs_profile_date_idx
  ON public.strength_logs (profile_id, performed_on);

COMMENT ON TABLE  public.strength_logs IS
  'Resistance-training sets: load / sets / reps / RIR per exercise group. Append-only; void via maintainer_void_strength(). RLS mirrors food_logs.';
COMMENT ON COLUMN public.strength_logs.id IS 'Primary key.';
COMMENT ON COLUMN public.strength_logs.profile_id IS 'Owner profile (FK profiles.id, ON DELETE CASCADE).';
COMMENT ON COLUMN public.strength_logs.performed_at IS 'When the set group was performed (timestamptz).';
COMMENT ON COLUMN public.strength_logs.performed_on IS 'GENERATED ALWAYS from performed_at AT TIME ZONE UTC — never insert/update; set performed_at.';
COMMENT ON COLUMN public.strength_logs.exercise IS 'Exercise name, e.g. trap_bar_deadlift, cable_lat_pulldown.';
COMMENT ON COLUMN public.strength_logs.modality IS 'Equipment type: barbell | machine | cable | dumbbell | bodyweight.';
COMMENT ON COLUMN public.strength_logs.load_value IS 'Load magnitude in load_unit (NULL for pure bodyweight).';
COMMENT ON COLUMN public.strength_logs.load_unit IS 'Unit of load_value: kg | lb | bodyweight. Never ''plates'' — resolve to the real stack unit first.';
COMMENT ON COLUMN public.strength_logs.sets IS 'Number of sets in this group.';
COMMENT ON COLUMN public.strength_logs.reps IS 'Reps per set in this group.';
COMMENT ON COLUMN public.strength_logs.rir IS 'Reps in reserve (proximity to failure).';
COMMENT ON COLUMN public.strength_logs.device_specific IS 'True when machine/cable loads are not comparable to free-weight loads.';
COMMENT ON COLUMN public.strength_logs.notes IS 'Free-text note for the set group.';
COMMENT ON COLUMN public.strength_logs.source IS 'Origin of the row, default ''skill''.';
COMMENT ON COLUMN public.strength_logs.voided_at IS
  'Soft-delete marker (mig 060 pattern). Non-NULL = excluded from every read/aggregation (WHERE voided_at IS NULL). Set via maintainer_void_strength().';
COMMENT ON COLUMN public.strength_logs.void_reason IS 'Why the row was voided (audit trail; >=5 chars, set by maintainer_void_strength()).';
COMMENT ON COLUMN public.strength_logs.created_at IS 'Row insert time.';

-- ── 2. RLS + grants (enable RLS BEFORE granting) ──

ALTER TABLE public.strength_logs ENABLE ROW LEVEL SECURITY;

-- Copied from food_logs: ONE FOR ALL policy on has_profile_access.
DROP POLICY IF EXISTS strength_logs_profile_access ON public.strength_logs;
CREATE POLICY strength_logs_profile_access ON public.strength_logs
  FOR ALL
  USING (has_profile_access(profile_id))
  WITH CHECK (has_profile_access(profile_id));

-- Mirror food_logs: authenticated only, NO DELETE, NOT healthspan_app.
GRANT SELECT, INSERT, UPDATE ON public.strength_logs TO authenticated;

-- ── 3. void RPC (mirrors maintainer_void_food, mig 060) ──

CREATE OR REPLACE FUNCTION public.maintainer_void_strength(p_id uuid, p_reason text)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO 'public'
AS $function$
DECLARE
  v_profile uuid;
  v_voided  timestamptz;
BEGIN
  IF p_reason IS NULL OR length(trim(p_reason)) < 5 THEN
    RAISE EXCEPTION 'void_reason required (>=5 chars) — the audit trail must say why';
  END IF;
  SELECT profile_id, voided_at INTO v_profile, v_voided
  FROM public.strength_logs WHERE id = p_id;
  IF v_profile IS NULL THEN
    RETURN jsonb_build_object('id', p_id, 'status', 'not_found');
  END IF;
  IF NOT (is_maintainer() AND has_profile_access(v_profile)) THEN
    RAISE EXCEPTION 'unauthorized: requires maintainer with family access to target profile';
  END IF;
  IF v_voided IS NOT NULL THEN
    RETURN jsonb_build_object('id', p_id, 'status', 'already_voided', 'voided_at', v_voided);
  END IF;
  UPDATE public.strength_logs
  SET voided_at = now(), void_reason = trim(p_reason)
  WHERE id = p_id;
  RETURN jsonb_build_object('id', p_id, 'status', 'voided');
END;
$function$;

REVOKE ALL ON FUNCTION public.maintainer_void_strength(uuid, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.maintainer_void_strength(uuid, text) TO authenticated, service_role;

-- ── 4. read-side filters ──
-- No view or query consumes strength_logs yet; every read introduced by the
-- brief's strength section / progression queries / analysis / lib.views MUST
-- carry `AND voided_at IS NULL` (a column with no filter is cosmetic — the
-- exact trap mig 060 called out).

-- ── verify ──

DO $$
DECLARE
  v_cols   int;
  v_rls    boolean;
  v_pol    int;
  v_fn     int;
  v_sel    int;
  v_ins    int;
  v_upd    int;
  v_del    int;
BEGIN
  -- table + void columns
  IF to_regclass('public.strength_logs') IS NULL THEN
    RAISE EXCEPTION 'Migration 063 verify: strength_logs missing';
  END IF;
  SELECT count(*) INTO v_cols FROM information_schema.columns
  WHERE table_schema='public' AND table_name='strength_logs'
    AND column_name IN ('voided_at','void_reason');
  IF v_cols <> 2 THEN
    RAISE EXCEPTION 'Migration 063 verify: expected 2 void columns, found %', v_cols;
  END IF;

  -- RLS enabled + the ALL policy
  SELECT relrowsecurity INTO v_rls FROM pg_class WHERE oid='public.strength_logs'::regclass;
  IF NOT v_rls THEN
    RAISE EXCEPTION 'Migration 063 verify: RLS not enabled on strength_logs';
  END IF;
  SELECT count(*) INTO v_pol FROM pg_policy
  WHERE polrelid='public.strength_logs'::regclass AND polname='strength_logs_profile_access' AND polcmd='*';
  IF v_pol <> 1 THEN
    RAISE EXCEPTION 'Migration 063 verify: FOR ALL policy missing (found %)', v_pol;
  END IF;

  -- grants: authenticated has SELECT/INSERT/UPDATE and NOT DELETE
  SELECT count(*) INTO v_sel FROM information_schema.role_table_grants
   WHERE table_schema='public' AND table_name='strength_logs' AND grantee='authenticated' AND privilege_type='SELECT';
  SELECT count(*) INTO v_ins FROM information_schema.role_table_grants
   WHERE table_schema='public' AND table_name='strength_logs' AND grantee='authenticated' AND privilege_type='INSERT';
  SELECT count(*) INTO v_upd FROM information_schema.role_table_grants
   WHERE table_schema='public' AND table_name='strength_logs' AND grantee='authenticated' AND privilege_type='UPDATE';
  SELECT count(*) INTO v_del FROM information_schema.role_table_grants
   WHERE table_schema='public' AND table_name='strength_logs' AND grantee='authenticated' AND privilege_type='DELETE';
  IF v_sel <> 1 OR v_ins <> 1 OR v_upd <> 1 THEN
    RAISE EXCEPTION 'Migration 063 verify: authenticated missing S/I/U (S=% I=% U=%)', v_sel, v_ins, v_upd;
  END IF;
  IF v_del <> 0 THEN
    RAISE EXCEPTION 'Migration 063 verify: authenticated must NOT hold DELETE (found %)', v_del;
  END IF;

  -- void RPC
  SELECT count(*) INTO v_fn FROM pg_proc
  WHERE pronamespace='public'::regnamespace AND proname='maintainer_void_strength';
  IF v_fn <> 1 THEN
    RAISE EXCEPTION 'Migration 063 verify: maintainer_void_strength missing (found %)', v_fn;
  END IF;

  RAISE NOTICE 'Migration 063 verify: OK — strength_logs + 2 void columns + RLS ALL policy + authenticated S/I/U (no DELETE) + maintainer_void_strength';
END $$;

COMMIT;
