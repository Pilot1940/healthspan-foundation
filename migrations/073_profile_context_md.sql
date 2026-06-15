-- =============================================================
-- 073_profile_context_md.sql
-- Per-profile CONTEXT body (markdown) → DB-backed source of truth for the targets /
-- coaching-voice / safety-rules / micronutrients / HR-zones that lib/context loads at
-- session start. Until now this lived ONLY in a bundle-baked context/<who>.context.md,
-- so every macro tweak meant rebuilding & re-shipping every bundle. With profiles.context_md
-- the maintainer edits ONE row in Supabase and every bundle (Cowork / claude.ai / App) reads
-- fresh on the next session start (lib.context.get_context already runs there). The file stays
-- as the offline fallback / dev-edit ergonomics; DB is primary.
--
-- RLS (Rule #3 — policy in the SAME migration that adds the writable surface):
--   * SELECT scope is already covered by the existing `profiles_access` policy
--     (FOR ALL TO authenticated USING has_profile_access(id)) — a person reads their own
--     row, the maintainer reads the family. No new SELECT policy needed.
--   * WRITE must be MAINTAINER-ONLY. But `profiles_access` is FOR ALL, so a non-maintainer
--     (Dea) could UPDATE context_md on HER OWN row. Postgres has no column-level RLS, so a
--     BEFORE-UPDATE TRIGGER guards the context columns: any change to context_md /
--     context_version by a non-maintainer RAISEs. The maintainer-only RPC is the clean write
--     path; the trigger is defence-in-depth against a direct UPDATE.
--
-- Personal context BODIES are NOT inlined here (transportable schema — same convention as the
-- mig 063 strength seed / mig 064 personal food-ref seed). Backfill runs AFTER apply via
-- scripts/backfill_073_context.py (idempotent), which reads context/*.context.md and calls the
-- RPC under a maintainer JWT claim. Idempotent / re-runnable.
-- =============================================================
BEGIN;

-- 1. Columns on the canonical person record. context_version is NOT NULL DEFAULT 1 so existing
--    rows are immediately well-formed; context_md stays NULL until backfilled (file fallback
--    covers the gap). context_updated_at is stamped by the trigger on every context write.
ALTER TABLE public.profiles
  ADD COLUMN IF NOT EXISTS context_md         text,
  ADD COLUMN IF NOT EXISTS context_version    integer     NOT NULL DEFAULT 1,
  ADD COLUMN IF NOT EXISTS context_updated_at timestamptz;

COMMENT ON COLUMN public.profiles.context_md IS
  'Per-profile context markdown — targets / coaching voice / safety rules / micronutrients / '
  'HR zones. DB-backed source of truth for lib.context.get_context (DB-first, then the bundle '
  'file fallback). SELECT inherits the profiles_access RLS policy; WRITE is maintainer-only '
  '(trg_guard_profile_context_update + maintainer_set_profile_context).';
COMMENT ON COLUMN public.profiles.context_version IS
  'Monotonic version of context_md, auto-bumped on every context write (RPC + trigger). Starts at 1.';
COMMENT ON COLUMN public.profiles.context_updated_at IS
  'When context_md was last written. Stamped by trg_guard_profile_context_update.';

-- 2. Column-scoped write guard. Fires on every profiles UPDATE but only acts when the context
--    columns change — so ordinary profile updates (display_name, is_minor_override, …) are
--    unaffected. A non-maintainer touching the context columns is rejected; a maintainer write
--    is stamped + version-monotonic-enforced (auto-bumps if the caller forgot).
CREATE OR REPLACE FUNCTION public.guard_profile_context_update()
RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER
SET search_path TO 'public', 'pg_catalog'
AS $function$
BEGIN
  IF NEW.context_md IS DISTINCT FROM OLD.context_md
     OR NEW.context_version IS DISTINCT FROM OLD.context_version THEN
    IF NOT is_maintainer() THEN
      RAISE EXCEPTION 'profile context is maintainer-only';
    END IF;
    NEW.context_updated_at := now();
    -- enforce monotonic version (auto-bump if the caller left it unchanged or lowered it)
    IF NEW.context_version <= OLD.context_version THEN
      NEW.context_version := OLD.context_version + 1;
    END IF;
  END IF;
  RETURN NEW;
END $function$;

DROP TRIGGER IF EXISTS trg_guard_profile_context_update ON public.profiles;
CREATE TRIGGER trg_guard_profile_context_update
  BEFORE UPDATE ON public.profiles
  FOR EACH ROW EXECUTE FUNCTION public.guard_profile_context_update();

-- 3. Maintainer-only write RPC — the clean edit path (PostgREST-callable for the App/claude.ai
--    bundles, psycopg2-callable for Cowork). Guards: maintainer-only, body floor (>=50 chars,
--    refuses an accidental truncation), profile must exist. Returns the new version. The
--    explicit version bump here + the trigger's monotonic guard collapse to a SINGLE +1.
CREATE OR REPLACE FUNCTION public.maintainer_set_profile_context(
  p_profile_id uuid,
  p_context_md text,
  p_reason     text DEFAULT NULL
) RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER
SET search_path TO 'public', 'pg_catalog'
AS $function$
DECLARE v_new_version integer;
BEGIN
  IF NOT is_maintainer() THEN
    RAISE EXCEPTION 'maintainer-only';
  END IF;
  IF p_context_md IS NULL OR length(trim(p_context_md)) < 50 THEN
    RAISE EXCEPTION 'context_md too short (<50 chars) — refusing';
  END IF;
  UPDATE public.profiles
     SET context_md      = p_context_md,
         context_version = context_version + 1
   WHERE id = p_profile_id
   RETURNING context_version INTO v_new_version;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'profile % not found', p_profile_id;
  END IF;
  RETURN v_new_version;
END $function$;

REVOKE ALL ON FUNCTION public.maintainer_set_profile_context(uuid, text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.maintainer_set_profile_context(uuid, text, text) TO authenticated;

DO $$
DECLARE n_cols int;
BEGIN
  SELECT count(*) INTO n_cols FROM information_schema.columns
   WHERE table_schema='public' AND table_name='profiles'
     AND column_name IN ('context_md','context_version','context_updated_at');
  IF n_cols <> 3 THEN
    RAISE EXCEPTION '073: expected 3 context columns on profiles, found %', n_cols;
  END IF;
  RAISE NOTICE '073: profiles.context_md + guard trigger + maintainer_set_profile_context() ready. '
               'Backfill bodies via scripts/backfill_073_context.py.';
END $$;

COMMIT;
