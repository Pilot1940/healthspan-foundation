-- =============================================================
-- 061_security_hygiene.sql
-- BACKLOG #21 (DB-side items): consolidated low-severity findings from the
-- 2026-06-10 deep scan.
--
--   1. Open INSERT policies (WITH CHECK true, role public) on media_inbox,
--      push_log, telegram_processed_updates, wearable_sync_errors. The
--      intended writers are service_role (telegram-webhook, whoop-webhook —
--      bypass RLS) except push_log, which the drain also INSERTs as the
--      authenticated drainer (inbox_drain.py brief-dedup record). anon holds
--      no table grants here, so the live exposure was any AUTHENTICATED
--      session (e.g. the minor's) inserting arbitrary rows for any profile —
--      a media_inbox insert triggers a paid drain run.
--        media_inbox / push_log → scoped to has_profile_access OR is_maintainer.
--        telegram_processed_updates / wearable_sync_errors → policy dropped
--        (service_role + postgres writers only; neither consults RLS).
--   2. Legacy mig-002 ingestion surface: ingest_health_artifact +
--      mint_ingestion_token were EXECUTE-granted to anon (Postgres default
--      PUBLIC grant). Superseded by the Telegram path; nothing calls them
--      from an anon/authenticated context (hs_ops mint-token runs as
--      postgres). DECISION: keep the functions, revoke anon + authenticated.
--   3. Dead system_config keys: ingest.whoop_screenshot.direct_write (gate
--      never read), supplements.journal_intake_map +
--      supplements.intake_source_priority (fossils of the dropped
--      source-priority design, mig 028). Deleted.
--   4. claim_inbox_cluster(uuid[]) RPC (mig 046) — never called; the drain
--      claims per-item via claim_inbox_item. Dropped (the misleading drain
--      comment is fixed in the same commit).
--
-- Apply: python3 scripts/hs_ops.py apply migrations/061_security_hygiene.sql
-- =============================================================
BEGIN;

-- ── 1. INSERT policies ──

DROP POLICY IF EXISTS mib_ins ON public.media_inbox;
CREATE POLICY mib_ins ON public.media_inbox
  FOR INSERT TO authenticated
  WITH CHECK (has_profile_access(profile_id) OR is_maintainer());

DROP POLICY IF EXISTS plo_ins ON public.push_log;
CREATE POLICY plo_ins ON public.push_log
  FOR INSERT TO authenticated
  WITH CHECK (has_profile_access(profile_id) OR is_maintainer());

DROP POLICY IF EXISTS tpu_ins ON public.telegram_processed_updates;

DROP POLICY IF EXISTS wearable_sync_errors_ins ON public.wearable_sync_errors;

-- ── 2. legacy mig-002 surface: revoke anon + authenticated ──

DO $$
DECLARE
  r record;
BEGIN
  FOR r IN
    SELECT p.oid::regprocedure AS sig
    FROM pg_proc p
    WHERE p.pronamespace = 'public'::regnamespace
      AND p.proname IN ('ingest_health_artifact', 'mint_ingestion_token')
  LOOP
    EXECUTE format('REVOKE ALL ON FUNCTION %s FROM PUBLIC, anon, authenticated', r.sig);
  END LOOP;
END $$;

-- ── 3. dead config keys ──

DELETE FROM public.system_config
WHERE key IN ('ingest.whoop_screenshot.direct_write',
              'supplements.journal_intake_map',
              'supplements.intake_source_priority');

-- ── 4. unused claim_inbox_cluster RPC ──

DROP FUNCTION IF EXISTS public.claim_inbox_cluster(uuid[]);

-- ── verify ──

DO $$
DECLARE
  v_open int;
  v_ins  int;
  v_anon int;
  v_keys int;
  v_fn   int;
BEGIN
  SELECT count(*) INTO v_open FROM pg_policies
  WHERE schemaname = 'public'
    AND tablename IN ('media_inbox','push_log','telegram_processed_updates','wearable_sync_errors')
    AND cmd = 'INSERT' AND with_check = 'true';
  IF v_open <> 0 THEN
    RAISE EXCEPTION 'Migration 061 verify: % open INSERT policies remain', v_open;
  END IF;

  SELECT count(*) INTO v_ins FROM pg_policies
  WHERE schemaname = 'public' AND tablename IN ('media_inbox','push_log')
    AND cmd = 'INSERT' AND with_check LIKE '%has_profile_access%';
  IF v_ins <> 2 THEN
    RAISE EXCEPTION 'Migration 061 verify: expected 2 scoped INSERT policies, found %', v_ins;
  END IF;

  SELECT count(*) INTO v_anon
  FROM pg_proc p
  CROSS JOIN LATERAL aclexplode(COALESCE(p.proacl, acldefault('f', p.proowner))) acl
  JOIN pg_roles g ON g.oid = acl.grantee
  WHERE p.pronamespace = 'public'::regnamespace
    AND p.proname IN ('ingest_health_artifact','mint_ingestion_token')
    AND g.rolname IN ('anon','authenticated');
  IF v_anon <> 0 THEN
    RAISE EXCEPTION 'Migration 061 verify: legacy ingestion fns still granted to anon/authenticated (%)', v_anon;
  END IF;

  SELECT count(*) INTO v_keys FROM public.system_config
  WHERE key IN ('ingest.whoop_screenshot.direct_write',
                'supplements.journal_intake_map',
                'supplements.intake_source_priority');
  IF v_keys <> 0 THEN
    RAISE EXCEPTION 'Migration 061 verify: % dead config keys remain', v_keys;
  END IF;

  SELECT count(*) INTO v_fn FROM pg_proc
  WHERE pronamespace = 'public'::regnamespace AND proname = 'claim_inbox_cluster';
  IF v_fn <> 0 THEN
    RAISE EXCEPTION 'Migration 061 verify: claim_inbox_cluster still exists';
  END IF;

  RAISE NOTICE 'Migration 061 verify: OK — INSERT policies scoped/dropped, legacy fns revoked, dead keys deleted, claim_inbox_cluster dropped';
END $$;

COMMIT;
