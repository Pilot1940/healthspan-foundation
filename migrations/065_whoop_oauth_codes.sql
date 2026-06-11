-- =============================================================
-- 065_whoop_oauth_codes.sql
-- Telegram-driven WHOOP re-auth: one-time, expiring tickets that carry the OAuth
-- `state` so a user can reconnect WHOOP by tapping a button in Telegram (no CLI,
-- no localhost). The ticket — NOT the raw profile_id — travels in the URL, so a
-- shared/leaked link can't attach a stranger's WHOOP to someone's profile.
--
-- Flow: telegram-webhook / whoop-webhook mint a ticket → send a "Reconnect WHOOP"
-- URL button → the whoop-oauth Edge function peeks the ticket on entry, then
-- consumes it (single-use) on the WHOOP callback and stores the tokens.
--
-- The Edge functions use the service_role key (bypasses RLS); RLS here is just the
-- belt-and-braces so authenticated end-users can't read/forge tickets.
--
-- Apply: python3 scripts/hs_ops.py apply migrations/065_whoop_oauth_codes.sql
-- =============================================================
BEGIN;

CREATE TABLE IF NOT EXISTS public.whoop_oauth_codes (
  code        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  profile_id  uuid NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  created_at  timestamptz NOT NULL DEFAULT now(),
  expires_at  timestamptz NOT NULL,
  used_at     timestamptz
);

CREATE INDEX IF NOT EXISTS whoop_oauth_codes_profile_idx
  ON public.whoop_oauth_codes (profile_id, created_at DESC);

COMMENT ON TABLE  public.whoop_oauth_codes IS
  'One-time, expiring tickets carrying the WHOOP OAuth state for the Telegram reconnect flow (mig 065). Minted by the bot/webhook, consumed once by whoop-oauth on the WHOOP callback. service_role only in practice.';
COMMENT ON COLUMN public.whoop_oauth_codes.code IS 'The ticket (also the OAuth state param). Single-use.';
COMMENT ON COLUMN public.whoop_oauth_codes.profile_id IS 'Profile this consent is for (FK profiles.id, ON DELETE CASCADE).';
COMMENT ON COLUMN public.whoop_oauth_codes.created_at IS 'Mint time (also the alert-debounce clock).';
COMMENT ON COLUMN public.whoop_oauth_codes.expires_at IS 'Ticket expiry (mint + whoop.oauth_ticket_ttl_min).';
COMMENT ON COLUMN public.whoop_oauth_codes.used_at IS 'Set when the WHOOP callback consumes the ticket; non-NULL = spent.';

ALTER TABLE public.whoop_oauth_codes ENABLE ROW LEVEL SECURITY;

-- Maintainer-only visibility (service_role bypasses RLS for the Edge functions).
-- end-users / minors can neither read nor forge tickets.
DROP POLICY IF EXISTS whoop_oauth_codes_maint ON public.whoop_oauth_codes;
CREATE POLICY whoop_oauth_codes_maint ON public.whoop_oauth_codes
  FOR ALL USING (is_maintainer()) WITH CHECK (is_maintainer());

-- Config (rule #1 — thresholds in system_config, not code).
INSERT INTO public.system_config (key, value, is_active) VALUES
  ('whoop.oauth_ticket_ttl_min', '30', true),
  ('whoop.reauth_alert_debounce_hours', '24', true)
ON CONFLICT (key) DO NOTHING;

-- ── verify ──
DO $$
DECLARE v_rls boolean; v_pol int; v_cfg int;
BEGIN
  IF to_regclass('public.whoop_oauth_codes') IS NULL THEN
    RAISE EXCEPTION 'Migration 065 verify: whoop_oauth_codes missing';
  END IF;
  SELECT relrowsecurity INTO v_rls FROM pg_class WHERE oid='public.whoop_oauth_codes'::regclass;
  IF NOT v_rls THEN RAISE EXCEPTION 'Migration 065 verify: RLS not enabled'; END IF;
  SELECT count(*) INTO v_pol FROM pg_policy WHERE polrelid='public.whoop_oauth_codes'::regclass;
  IF v_pol <> 1 THEN RAISE EXCEPTION 'Migration 065 verify: expected 1 policy, found %', v_pol; END IF;
  SELECT count(*) INTO v_cfg FROM public.system_config
   WHERE key IN ('whoop.oauth_ticket_ttl_min','whoop.reauth_alert_debounce_hours');
  IF v_cfg <> 2 THEN RAISE EXCEPTION 'Migration 065 verify: config keys missing (found %)', v_cfg; END IF;
  RAISE NOTICE 'Migration 065 verify: OK — whoop_oauth_codes + RLS + 2 config keys';
END $$;

COMMIT;
