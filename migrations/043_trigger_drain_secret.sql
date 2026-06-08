-- 043_trigger_drain_secret.sql
-- Two changes:
--   1. Seed the system_config row for trigger_drain.last_dispatch_ts so the atomic
--      CAS UPDATE in trigger-drain/index.ts has a row to match against.
--      The row is initialised to the epoch (1970-01-01) so the first call always wins.
--   2. Update fn_media_inbox_notify to forward the TRIGGER_DRAIN_SECRET in the
--      Authorization header instead of the public anon key.
--      The secret value is read from Supabase vault at call time via
--      vault.decrypted_secrets. If the vault secret is not yet set this falls back
--      to an empty string and the function will reject the call with 401 — safe fail.
--
-- Deploy steps (once per environment):
--   a. supabase secrets set TRIGGER_DRAIN_SECRET=<strong-random-value>
--   b. Apply this migration: python scripts/hs_ops.py apply migrations/043_trigger_drain_secret.sql
--   c. Redeploy the edge function: supabase functions deploy trigger-drain

-- 1. Seed the dedup row (idempotent)
INSERT INTO public.system_config (key, value, description, category, is_active, updated_at)
VALUES (
  'trigger_drain.last_dispatch_ts',
  to_jsonb('1970-01-01T00:00:00Z'::text),
  'ISO timestamp of the last trigger-drain dispatch; used for atomic CAS dedup.',
  'drain',
  true,
  '1970-01-01T00:00:00Z'::timestamptz
)
ON CONFLICT (key) DO UPDATE
  SET updated_at = '1970-01-01T00:00:00Z'::timestamptz;

-- 2. Replace fn_media_inbox_notify to send TRIGGER_DRAIN_SECRET via vault lookup.
--    pg_net fires the request asynchronously — the trigger returns immediately.
CREATE OR REPLACE FUNCTION public.fn_media_inbox_notify()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, vault
AS $$
DECLARE
  v_secret text;
BEGIN
  IF NEW.status = 'pending' THEN
    -- Fetch the shared secret from Supabase vault (requires pg_net + vault extension).
    -- Falls back to empty string if the secret is not yet provisioned.
    BEGIN
      SELECT decrypted_secret
        INTO v_secret
        FROM vault.decrypted_secrets
       WHERE name = 'TRIGGER_DRAIN_SECRET'
       LIMIT 1;
    EXCEPTION WHEN OTHERS THEN
      v_secret := '';
    END;

    PERFORM net.http_post(
      url     := 'https://dsnydskkjwziynwmzfkh.supabase.co/functions/v1/trigger-drain',
      body    := jsonb_build_object('inbox_id', NEW.id),
      headers := jsonb_build_object(
        'Content-Type',  'application/json',
        'Authorization', 'Bearer ' || coalesce(v_secret, '')
      ),
      timeout_milliseconds := 3000
    );
  END IF;
  RETURN NEW;
END;
$$;

-- Trigger is already registered from migration 042; no DROP/CREATE needed.
-- The REPLACE above updates the function body in place.
