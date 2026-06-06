-- =============================================================
-- 029_telegram_ingestion.sql  (FEATURE — Phase 1 Telegram ingestion tables + RLS)
-- 2026-06-06.
--
-- Context: 028 (2026-06-04) dropped `user_telegram_links` calling Telegram an
-- "abandoned-vision fossil, not in the stack". The SCOPE-LOCKED spec
-- HealthSpan-Telegram-Ingestion-Architecture.md (2026-06-05) reverses that decision.
-- This migration builds the production-grade replacement: five new tables, RLS on
-- all, column comments in 016-style (MEANING | UNIT | TRAP).
--
-- Tables created:
--   telegram_link_codes        — one-time codes PC mints before a chat_id is known.
--   telegram_identities        — chat_id → profile_id binding (the live auth map).
--   telegram_processed_updates — inbound update_id idempotency latch.
--   media_inbox                — photo/text queue drained by Phase-3 Routine.
--   push_log                   — outbound push idempotency + debounce record.
--
-- Auth model: Edge fn writes via service_role (same pattern as whoop-webhook; no
-- user session in an inbound webhook). healthspan_app (Phase-3 Routine) reads via
-- SET ROLE authenticated + JWT → RLS applies normally. Both paths respected below.
-- =============================================================
BEGIN;

-- ============ telegram_link_codes ============
-- PC mints a code out-of-band (hs_ops mint-link-code <profile_id>); the recipient
-- sends it to the bot to bind their Telegram chat_id → profile. Single-use + expiring.
CREATE TABLE public.telegram_link_codes (
  code          text        NOT NULL,
  profile_id    uuid        NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  created_at    timestamptz NOT NULL DEFAULT now(),
  expires_at    timestamptz NOT NULL DEFAULT now() + INTERVAL '7 days',
  used_at       timestamptz,
  CONSTRAINT telegram_link_codes_pkey PRIMARY KEY (code)
);

ALTER TABLE public.telegram_link_codes ENABLE ROW LEVEL SECURITY;

CREATE POLICY tlc_sel ON public.telegram_link_codes FOR SELECT USING (is_maintainer());
CREATE POLICY tlc_ins ON public.telegram_link_codes FOR INSERT WITH CHECK (is_maintainer());
CREATE POLICY tlc_upd ON public.telegram_link_codes
  FOR UPDATE USING (is_maintainer()) WITH CHECK (is_maintainer());

COMMENT ON TABLE  public.telegram_link_codes IS
  'One-time activation codes PC mints out-of-band; sent to a recipient who DMs the bot '
  'to bind their Telegram chat_id to a HealthSpan profile. Single-use, 7-day default expiry.';
COMMENT ON COLUMN public.telegram_link_codes.code IS
  'The secret code string (PK). TRAP: treat as a credential — never log in plaintext; '
  'rotate on any leak.';
COMMENT ON COLUMN public.telegram_link_codes.profile_id IS
  'FK profiles.id — the profile this code will activate. Set at mint time; determines '
  'whose data the linked chat_id may submit.';
COMMENT ON COLUMN public.telegram_link_codes.created_at IS
  'Mint timestamp (timestamptz).';
COMMENT ON COLUMN public.telegram_link_codes.expires_at IS
  'Hard expiry (timestamptz). Edge fn rejects expired codes; default 7 days from mint.';
COMMENT ON COLUMN public.telegram_link_codes.used_at IS
  'Redemption timestamp (timestamptz). NULL = not yet redeemed. TRAP: non-NULL used_at '
  'means the code is spent; a second redemption attempt is silently rejected.';

-- ============ telegram_identities ============
-- The live Telegram chat_id → profile_id map. One row per linked person.
CREATE TABLE public.telegram_identities (
  chat_id       bigint      NOT NULL,
  profile_id    uuid        NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  display_name  text,
  is_minor      boolean     NOT NULL DEFAULT false,
  linked_at     timestamptz NOT NULL DEFAULT now(),
  link_code     text,
  status        text        NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending','active','revoked')),
  CONSTRAINT telegram_identities_pkey PRIMARY KEY (chat_id)
);

ALTER TABLE public.telegram_identities ENABLE ROW LEVEL SECURITY;

CREATE POLICY tid_sel ON public.telegram_identities
  FOR SELECT USING (has_profile_access(profile_id) OR is_maintainer());
CREATE POLICY tid_ins ON public.telegram_identities
  FOR INSERT WITH CHECK (is_maintainer());
CREATE POLICY tid_upd ON public.telegram_identities
  FOR UPDATE USING (is_maintainer()) WITH CHECK (is_maintainer());

COMMENT ON TABLE  public.telegram_identities IS
  'Maps Telegram chat_id to a HealthSpan profile. Populated when a link code is redeemed '
  '(pending → active). One row per linked person. status transitions: pending → active → revoked.';
COMMENT ON COLUMN public.telegram_identities.chat_id IS
  'Telegram chat_id (bigint PK). TRAP: NOT a phone number — Telegram does not reliably '
  'expose the phone. This is the stable identifier the bot uses for routing.';
COMMENT ON COLUMN public.telegram_identities.profile_id IS
  'FK profiles.id — the HealthSpan profile this chat belongs to. RLS: a chat writes ONLY '
  'its own profile_id; cross-profile writes (e.g. Dea → PC) are blocked by the Edge fn '
  'identity lookup, not only by the policy.';
COMMENT ON COLUMN public.telegram_identities.display_name IS
  'Telegram first+last name at link time (informational only; not updated on rename).';
COMMENT ON COLUMN public.telegram_identities.is_minor IS
  'True if the linked profile is a child. Edge fn uses this to apply minor-safe framing '
  'on confirmations (growth/performance language, never deficit/restriction).';
COMMENT ON COLUMN public.telegram_identities.linked_at IS
  'Timestamp the identity was activated (timestamptz).';
COMMENT ON COLUMN public.telegram_identities.link_code IS
  'The activation code used (audit trail). References telegram_link_codes.code but is NOT '
  'a FK — the code row may be pruned; this column is historical record only.';
COMMENT ON COLUMN public.telegram_identities.status IS
  'Lifecycle: pending (pre-activation) | active (code redeemed, data accepted) | revoked '
  '(access withdrawn). TRAP: only active rows are allowed to submit data; pending and '
  'revoked are rejected by the Edge fn before any write.';

-- ============ telegram_processed_updates ============
-- Inbound idempotency latch. Telegram retries unsatisfied updates reusing the same
-- update_id. Persisting seen update_ids prevents double-processing.
-- ORDERING RULE: this row is written ONLY AFTER media_inbox insert succeeds, so a
-- partial failure is retried by Telegram (not suppressed).
CREATE TABLE public.telegram_processed_updates (
  update_id     bigint      NOT NULL,
  received_at   timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT telegram_processed_updates_pkey PRIMARY KEY (update_id)
);

ALTER TABLE public.telegram_processed_updates ENABLE ROW LEVEL SECURITY;

CREATE POLICY tpu_sel ON public.telegram_processed_updates
  FOR SELECT USING (is_maintainer());
CREATE POLICY tpu_ins ON public.telegram_processed_updates
  FOR INSERT WITH CHECK (true);

COMMENT ON TABLE  public.telegram_processed_updates IS
  'Inbound idempotency latch: one row per Telegram update_id successfully processed. '
  'TRAP: written ONLY AFTER media_inbox insert succeeds (not before) — a partial failure '
  'leaves no row so Telegram retry is not suppressed. PK conflict on concurrent replay = safe no-op.';
COMMENT ON COLUMN public.telegram_processed_updates.update_id IS
  'Telegram update_id (bigint PK). Telegram reuses this id on retries; the PK constraint '
  'makes a concurrent INSERT of the same id conflict and return, not duplicate.';
COMMENT ON COLUMN public.telegram_processed_updates.received_at IS
  'Timestamp the update was first successfully processed (timestamptz). Not Telegram send time.';

-- ============ media_inbox ============
-- Durable queue the Phase-3 Routine drains. One row per inbound photo or text.
CREATE TABLE public.media_inbox (
  id            uuid        NOT NULL DEFAULT gen_random_uuid(),
  profile_id    uuid        NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  chat_id       bigint      NOT NULL,
  kind          text        NOT NULL DEFAULT 'unknown'
                  CHECK (kind IN ('food','workout','lab','dexa','unknown')),
  storage_path  text,
  caption       text,
  status        text        NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending','processing','done','failed','staged')),
  whoop_id      text,
  created_at    timestamptz NOT NULL DEFAULT now(),
  processed_at  timestamptz,
  result_ref    uuid,
  CONSTRAINT media_inbox_pkey PRIMARY KEY (id)
);

ALTER TABLE public.media_inbox ENABLE ROW LEVEL SECURITY;

CREATE POLICY mib_sel ON public.media_inbox
  FOR SELECT USING (has_profile_access(profile_id) OR is_maintainer());
CREATE POLICY mib_ins ON public.media_inbox
  FOR INSERT WITH CHECK (true);
CREATE POLICY mib_upd ON public.media_inbox
  FOR UPDATE USING (has_profile_access(profile_id) OR is_maintainer())
  WITH CHECK (has_profile_access(profile_id) OR is_maintainer());

COMMENT ON TABLE  public.media_inbox IS
  'Durable inbound queue: one row per Telegram photo/text from an active identity. '
  'Phase-3 Routine drains this (pending → processing → done/failed/staged). '
  'TRAP: storage_path NULL = text-only or download failed; Routine re-fetches if kind ≠ unknown.';
COMMENT ON COLUMN public.media_inbox.id IS
  'PK (gen_random_uuid). Referenced by result_ref in downstream tables (food_logs, biomarkers).';
COMMENT ON COLUMN public.media_inbox.profile_id IS
  'FK profiles.id — the profile that submitted this item. Derived ONLY from '
  'telegram_identities(chat_id → profile_id); NEVER from caption or message text (injection rule).';
COMMENT ON COLUMN public.media_inbox.chat_id IS
  'Telegram chat_id that sent this item (denormalised; used by Routine to send confirmation reply).';
COMMENT ON COLUMN public.media_inbox.kind IS
  'Guessed media type: food | workout | lab | dexa | unknown. Guessed from caption keywords '
  'at ingest; overridable by the Routine after vision extraction. Default unknown.';
COMMENT ON COLUMN public.media_inbox.storage_path IS
  'Path in the health-media private Storage bucket (object key; signed URLs only). '
  'NULL = text-only row or Telegram getFile failed; Routine must re-fetch if kind ≠ unknown.';
COMMENT ON COLUMN public.media_inbox.caption IS
  'Raw caption or message text — stored as DATA only, never executed. '
  'TRAP: injection rule — "delete my logs" in a caption is surfaced, not run.';
COMMENT ON COLUMN public.media_inbox.status IS
  'Queue state: pending (enqueued, awaiting Routine) | processing (Routine locked) | '
  'done (extracted + written) | failed (Routine gave up after retries) | '
  'staged (low-confidence → stg_*_review). TRAP: only the Routine transitions away from pending.';
COMMENT ON COLUMN public.media_inbox.whoop_id IS
  'WHOOP entity id if the Routine correlates this item to a specific workout/sleep/cycle. '
  'Text, not FK (WHOOP ids are strings). NULL until the Routine sets it.';
COMMENT ON COLUMN public.media_inbox.created_at IS
  'Enqueue timestamp (timestamptz). Use for Routine SLA monitoring — not Telegram send time.';
COMMENT ON COLUMN public.media_inbox.processed_at IS
  'Timestamp the Routine finished processing (timestamptz). NULL = not yet processed.';
COMMENT ON COLUMN public.media_inbox.result_ref IS
  'UUID of the downstream row written by the Routine (food_logs.id, biomarkers.id, etc.). '
  'NULL until status = done.';

-- ============ push_log ============
-- Outbound idempotency + debounce ledger. One row per push attempt.
CREATE TABLE public.push_log (
  id            uuid        NOT NULL DEFAULT gen_random_uuid(),
  profile_id    uuid        NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  push_type     text        NOT NULL,
  subject_id    text,
  sent_at       timestamptz NOT NULL DEFAULT now(),
  status        text        NOT NULL DEFAULT 'sent'
                  CHECK (status IN ('sent','failed','suppressed')),
  CONSTRAINT push_log_pkey PRIMARY KEY (id)
);

ALTER TABLE public.push_log ENABLE ROW LEVEL SECURITY;

CREATE POLICY plo_sel ON public.push_log FOR SELECT USING (is_maintainer());
CREATE POLICY plo_ins ON public.push_log FOR INSERT WITH CHECK (true);
CREATE POLICY plo_upd ON public.push_log
  FOR UPDATE USING (is_maintainer()) WITH CHECK (is_maintainer());

COMMENT ON TABLE  public.push_log IS
  'Outbound Telegram push idempotency + debounce ledger. One row per push attempt '
  '(sent, failed, or suppressed). Debounce key: (profile_id, push_type, subject_id) '
  'within a rolling window. Maintainer-only SELECT; INSERT by service role.';
COMMENT ON COLUMN public.push_log.id IS
  'PK (gen_random_uuid).';
COMMENT ON COLUMN public.push_log.profile_id IS
  'FK profiles.id — whose Telegram chat was targeted by this push.';
COMMENT ON COLUMN public.push_log.push_type IS
  'Push category (e.g. recovery_landed, workout_logged, media_done, morning_digest). '
  'First dimension of the debounce key.';
COMMENT ON COLUMN public.push_log.subject_id IS
  'The entity being pushed about (whoop_id, media_inbox.id as text, date string). '
  'Combined with push_type + profile_id for debounce. NULL for category-level pushes.';
COMMENT ON COLUMN public.push_log.sent_at IS
  'Timestamp the push was attempted (timestamptz). Debounce window anchor.';
COMMENT ON COLUMN public.push_log.status IS
  'Delivery outcome: sent | failed | suppressed (inside debounce window or quiet hours). '
  'TRAP: suppressed is intentional — do not count as an error in monitoring.';

-- ============ Grants — match 010/014 pattern ============
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY[
    'telegram_link_codes','telegram_identities','telegram_processed_updates',
    'media_inbox','push_log'
  ] LOOP
    EXECUTE format('REVOKE DELETE, TRUNCATE ON public.%I FROM authenticated', t);
    EXECUTE format('REVOKE DELETE, TRUNCATE ON public.%I FROM healthspan_app', t);
    EXECUTE format('REVOKE ALL PRIVILEGES ON public.%I FROM anon', t);
  END LOOP;
  RAISE NOTICE '029: DELETE/TRUNCATE revoked from authenticated + healthspan_app on all 5 new tables; ALL revoked from anon.';
END $$;

-- ============ Verify ============
DO $$
DECLARE n int;
BEGIN
  SELECT count(*) INTO n FROM pg_tables
  WHERE schemaname='public'
    AND tablename IN (
      'telegram_link_codes','telegram_identities',
      'telegram_processed_updates','media_inbox','push_log'
    );
  IF n <> 5 THEN
    RAISE EXCEPTION '029: expected 5 new tables, found %', n;
  END IF;

  SELECT count(*) INTO n FROM pg_class c
  JOIN pg_namespace ns ON ns.oid = c.relnamespace
  WHERE ns.nspname='public'
    AND c.relname IN (
      'telegram_link_codes','telegram_identities',
      'telegram_processed_updates','media_inbox','push_log'
    )
    AND c.relrowsecurity = true;
  IF n <> 5 THEN
    RAISE EXCEPTION '029: RLS not enabled on all 5 tables (only % have RLS on)', n;
  END IF;

  RAISE NOTICE '029 OK — 5 tables created, RLS enabled on all 5.';
END $$;

COMMIT;
