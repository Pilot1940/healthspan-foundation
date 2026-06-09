-- =============================================================
-- 051_supplements_learned_stamps.sql
-- Stamps for auto-learned supplements (learn-on-clarify). When the reply-to-clarify loop
-- resolves a supplement that isn't in the catalog, the drain auto-adds it (PC only; minors
-- route to PC). These columns let the maintainer SEE and prune what got learned —
-- the guardrail against a "context mess" (mirrors food_reference.source/verified).
--
-- Additive only (ADD COLUMN IF NOT EXISTS); no RLS change (supplements already has policies).
-- =============================================================
BEGIN;

ALTER TABLE public.supplements
  ADD COLUMN IF NOT EXISTS source   text,
  ADD COLUMN IF NOT EXISTS verified boolean NOT NULL DEFAULT false;

COMMENT ON COLUMN public.supplements.source IS
  'How the row was created: NULL/seed = curated, ''learned'' = auto-added via clarify loop.';
COMMENT ON COLUMN public.supplements.verified IS
  'Maintainer has reviewed/OKed this row. Auto-learned rows start false until PC verifies.';

-- Index the maintainer "recently learned, unverified" review surface.
CREATE INDEX IF NOT EXISTS idx_supplements_learned_unverified
  ON public.supplements (created_at DESC)
  WHERE source = 'learned' AND verified = false;

COMMIT;
