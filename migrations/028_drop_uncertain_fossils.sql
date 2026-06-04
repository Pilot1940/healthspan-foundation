-- =============================================================
-- 028_drop_uncertain_fossils.sql  (CLEANUP — PC ruling 2026-06-04)
-- Four of the six tables left UNCERTAIN in the 2026-06-03 health check, ruled DEAD by PC:
--   user_telegram_links   — abandoned-vision fossil. Push is Google Chat via webhook URLs
--                           in Supabase secrets, NOT Telegram; Telegram is not in the stack.
--   locations             — shared-reference locations; travel_log is the live model.
--   log_type_config       — logging-types feature that never shipped.
--   source_priority_config— source-ranking config that was never built out.
-- All four are empty and (post-026, which dropped their child tables) carry NO inbound FK.
--
-- KEPT (NOT dropped) — the other two UNCERTAIN tables, ruled live by PC:
--   audit_log         — reserved for a future write-mutation audit (maintainer
--                       corrections/promotions); query_audit covers reads only.
--   canonical_aliases — lab-name/marker alias mapping (SGPT→ALT, etc.); serves rule #3 as
--                       more labs flow in.
-- =============================================================
BEGIN;

DO $$
DECLARE n int;
BEGIN
  SELECT count(*) INTO n FROM (
    SELECT 1 FROM user_telegram_links
    UNION ALL SELECT 1 FROM locations
    UNION ALL SELECT 1 FROM log_type_config
    UNION ALL SELECT 1 FROM source_priority_config
  ) s;
  IF n > 0 THEN
    RAISE EXCEPTION '028: one of the fossils is non-empty (% rows) — re-review before dropping', n;
  END IF;
END $$;

DROP TABLE
  public.user_telegram_links,
  public.locations,
  public.log_type_config,
  public.source_priority_config;

DO $$ BEGIN RAISE NOTICE '028: dropped 4 uncertain fossils (kept audit_log + canonical_aliases)'; END $$;

COMMIT;
