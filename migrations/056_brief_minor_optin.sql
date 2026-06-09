-- =============================================================
-- 056_brief_minor_optin.sql
-- Per-profile consent allowlist for sending the daily brief to a MINOR.
--
-- By default minors receive only per-item log confirmations ("Tracked: …") —
-- never the full daily brief (food totals + WHOOP + plan). PC, as the maintainer
-- and Dea's father, consented (2026-06-09) to Dea's minor profile receiving the
-- minor-SAFE brief, including her WHOOP data. The brief content already softens
-- for is_minor (no deficit language, no Viome flags, no maintainer "learned" list).
--
-- This seeds `brief.minor_optin_profile_ids` — a JSON array of profile_ids that
-- are minors but ARE allowed a brief. The drain (post-log trigger) and the
-- send-brief --all (scheduled trigger) both consult it; a minor NOT in the list
-- (e.g. the dormant Dev slot, or any future minor) stays brief-off by default,
-- so consent is explicit per profile (CLAUDE rule #1 — config, not hardcode).
-- To revoke: remove the id from the array (or set the value to []).
-- =============================================================
BEGIN;

INSERT INTO public.system_config (key, value, description, category, is_active)
VALUES (
  'brief.minor_optin_profile_ids',
  '["3eed5503-a26f-4b88-bb76-075208fa5de3"]'::jsonb,  -- Dea Singh Chitalkar (minor; PC-consented 2026-06-09)
  'Profile_ids of MINORS explicitly consented (by the maintainer) to receive the daily brief. Minors not listed get log confirmations only.',
  'brief',
  true
)
ON CONFLICT (key) DO UPDATE
  SET value = EXCLUDED.value,
      description = EXCLUDED.description,
      is_active = true,
      updated_at = now();

COMMIT;
