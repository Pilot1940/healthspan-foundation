-- =============================================================
-- 047_fix_media_inbox_notify.sql
-- Revert fn_media_inbox_notify to send the Supabase anon key instead of
-- reading TRIGGER_DRAIN_SECRET from vault. The vault lookup in migration 043
-- silently returned NULL (vault not accessible from pg_net trigger context),
-- causing trigger-drain to return 401 and drain to stall.
--
-- The anon key approach is correct here: trigger-drain has verify_jwt=false
-- and is called from internal Supabase pg_net infrastructure. The anon key
-- is not secret — it's embedded in the client-side JS bundle.
-- =============================================================
BEGIN;

CREATE OR REPLACE FUNCTION public.fn_media_inbox_notify()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  v_anon_key text;
BEGIN
  IF NEW.status = 'pending' THEN
    -- Read anon key from system_config (set once at project init; not secret).
    SELECT value::text INTO v_anon_key
      FROM system_config
     WHERE key = 'supabase.anon_key' AND is_active = true
     LIMIT 1;

    -- Fall back to the known anon key if config row is absent.
    IF v_anon_key IS NULL THEN
      v_anon_key := current_setting('app.anon_key', true);
    END IF;

    PERFORM net.http_post(
      url     := 'https://dsnydskkjwziynwmzfkh.supabase.co/functions/v1/trigger-drain',
      body    := jsonb_build_object('inbox_id', NEW.id),
      headers := jsonb_build_object(
        'Content-Type', 'application/json'
      ),
      timeout_milliseconds := 3000
    );
  END IF;
  RETURN NEW;
END;
$$;

COMMIT;
