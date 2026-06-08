-- 042_trigger_drain_webhook.sql
-- pg_net trigger: fire trigger-drain Edge function on every new media_inbox row.
-- The Edge function handles deduplication, so firing per-row is safe.
--
-- NOTE: SUPERSEDED by migration 047 — fn_media_inbox_notify now posts to trigger-drain
-- with NO auth header (the vault/Bearer scheme was unreachable from pg_net context).
-- The anon-key Bearer below has been REDACTED (it was the public anon JWT, but literal
-- tokens must not ship in the transportable skill bundle — leak guard). This file is a
-- historical record; the live function is the 047 version.

CREATE OR REPLACE FUNCTION public.fn_media_inbox_notify()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
  IF NEW.status = 'pending' THEN
    PERFORM net.http_post(
      url     := 'https://dsnydskkjwziynwmzfkh.supabase.co/functions/v1/trigger-drain',
      body    := jsonb_build_object('inbox_id', NEW.id),
      headers := jsonb_build_object(
        'Content-Type',  'application/json',
        'Authorization', 'Bearer <ANON_KEY_REDACTED_SEE_MIGRATION_047>'
      ),
      timeout_milliseconds := 3000
    );
  END IF;
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_media_inbox_notify ON public.media_inbox;

CREATE TRIGGER trg_media_inbox_notify
  AFTER INSERT ON public.media_inbox
  FOR EACH ROW
  EXECUTE FUNCTION public.fn_media_inbox_notify();
