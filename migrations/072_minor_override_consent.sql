-- Migration 072: record Dea's is_minor=false as an EXPLICIT, parent-consented override
--
-- Background: `is_minor` is a stored flag on telegram_identities (operative at runtime) +
-- context/dea.context.md (lib/context). Dea (relationship='child', DOB 2012-06-01 → 14) is
-- intentionally is_minor=false (adult coaching framing) per her father PC's consent. Nothing
-- recorded WHY, so a future maintainer couldn't tell intent from accident, and a future minor
-- could be adult-framed unnoticed (deep-scan finding F5).
--
-- This adds a provenance record on `profiles` (the canonical person record) + a note. The
-- operative flag stays telegram_identities.is_minor; this column declares a non-default value
-- is deliberate. A guard test (tests/integration/test_rls_view_isolation.py) enforces:
-- no child-relationship profile may have an ACTIVE identity is_minor=false without this override.
--
-- profiles already has RLS (no new policy needed — columns inherit it). Idempotent.

ALTER TABLE public.profiles ADD COLUMN IF NOT EXISTS is_minor_override boolean NOT NULL DEFAULT false;
ALTER TABLE public.profiles ADD COLUMN IF NOT EXISTS is_minor_override_note text;

COMMENT ON COLUMN public.profiles.is_minor_override IS
  'TRUE when this profile''s is_minor framing is an explicit, authorized override of the '
  'age/relationship default (e.g. maintainer/parent-consented adult coaching framing for a '
  '<18 child). The operative runtime flag remains telegram_identities.is_minor; this column '
  'records that a non-default value is intentional. Provenance in is_minor_override_note.';
COMMENT ON COLUMN public.profiles.is_minor_override_note IS
  'Who authorized the is_minor override, why, and when (free text). Set when is_minor_override=true.';

UPDATE public.profiles
   SET is_minor_override = true,
       is_minor_override_note =
         'Adult coaching framing (is_minor=false) authorized by father PC (maintainer, profile '
         '21f69003-46f8-4e1c-a928-b1f694ce4aff) — PERMANENT parent consent. Applies across '
         'telegram_identities.is_minor + context/dea.context.md. Recorded 2026-06-15.'
 WHERE id = '3eed5503-a26f-4b88-bb76-075208fa5de3'
   AND display_name ILIKE 'Dea%';
