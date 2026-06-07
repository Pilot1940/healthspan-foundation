-- 042_trigger_drain_webhook.sql
-- pg_net trigger: fire trigger-drain Edge function on every new media_inbox row.
-- The Edge function handles deduplication, so firing per-row is safe.
--
-- The anon key in the Authorization header is intentionally public-facing
-- (the same JWT used in client-side JS). The Edge function uses service_role
-- internally for its Supabase calls.

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
        'Authorization', 'Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImRzbnlkc2trand6aXlud216ZmtoIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzM3NTY5MjIsImV4cCI6MjA4OTMzMjkyMn0.3i68UT8_Nr2psZZKfEv5BMRaSpIx9n0bUMSVVrt4T1M'
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
