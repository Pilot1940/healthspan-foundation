-- 053_storage_drainer_read.sql
-- Root cause of "photos always brief / never read as images":
--   storage.objects has RLS ENABLED with ZERO policies, and `health-media` is a
--   PRIVATE bucket. The telegram-webhook Edge function uploads with the service-role
--   key (bypasses RLS), so objects land fine. But the DRAIN reads them as the
--   service-account USER `healthspan.drainer@chitalkar.com` (role `authenticated`),
--   which RLS denies — so get_signed_url() returns NULL, image_blocks is empty, and
--   the vision model never sees the pixels. Caption-less photos then classify as
--   `brief`; captioned photos get extracted from the CAPTION TEXT ONLY.
--
-- Fix: a single, tightly-scoped SELECT policy letting ONLY the drainer service
--   account read objects in the `health-media` bucket. End-users still have no
--   direct media access (unchanged — they never read media). service_role continues
--   to bypass RLS for uploads. Drainer UID is resolved dynamically by email (mirrors
--   migration 035) so no UUID is hardcoded.

DO $$
DECLARE
  v_drainer uuid;
BEGIN
  SELECT id INTO v_drainer
    FROM auth.users
   WHERE email = 'healthspan.drainer@chitalkar.com';

  IF v_drainer IS NULL THEN
    RAISE EXCEPTION '053: drainer auth user healthspan.drainer@chitalkar.com not found';
  END IF;

  DROP POLICY IF EXISTS drainer_read_health_media ON storage.objects;

  EXECUTE format($f$
    CREATE POLICY drainer_read_health_media
      ON storage.objects
      FOR SELECT
      TO authenticated
      USING (bucket_id = 'health-media' AND auth.uid() = %L)
  $f$, v_drainer);

  RAISE NOTICE '053: storage SELECT policy for drainer (%) on health-media created', v_drainer;
END $$;
