-- =============================================================
-- 009_context_notes.sql  (ADDITIVE — new table + RLS)
-- PROMPT 9. health_context_notes: per-profile narrative/state for the skill's
-- session memory. Notes hold NARRATIVE + REFERENCES to numbers, never the numbers
-- themselves (numbers always come from the DB). One 'current' superseding summary
-- per profile + short 'log' notes. RLS via has_profile_access().
-- =============================================================
BEGIN;

CREATE TABLE IF NOT EXISTS public.health_context_notes (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  profile_id    uuid NOT NULL REFERENCES public.profiles(id),
  kind          text NOT NULL CHECK (kind IN ('current','log')),
  summary       text NOT NULL,                          -- narrative / state ONLY
  refs          jsonb,                                  -- pointers to numbers ("see biomarkers 2026-06-02"), NOT the numbers
  s3_keys       text[],                                 -- optional external MD files on S3
  supersedes_id uuid REFERENCES public.health_context_notes(id),
  created_at    timestamptz NOT NULL DEFAULT now()
);

-- exactly one 'current' note per profile
CREATE UNIQUE INDEX IF NOT EXISTS uq_context_current
  ON public.health_context_notes(profile_id) WHERE kind='current';

-- helpful index for "last N log notes"
CREATE INDEX IF NOT EXISTS idx_context_profile_kind_time
  ON public.health_context_notes(profile_id, kind, created_at DESC);

-- RLS
ALTER TABLE public.health_context_notes ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS health_context_notes_profile_access ON public.health_context_notes;
CREATE POLICY health_context_notes_profile_access ON public.health_context_notes
  USING (has_profile_access(profile_id))
  WITH CHECK (has_profile_access(profile_id));

COMMIT;
