-- =============================================================
-- 021_query_audit.sql  (ADDITIVE — new table + RLS)
-- V3-2 / blueprint §4.2. Audit every AD-HOC (non-catalog) query the skill runs:
-- the intent, the SQL, its EXPLAIN plan, the quality verdict, the row count, and a
-- flagged bool. Catalog (pre-vetted lib/views) queries are NOT audited.
-- Created here RLS-scoped by profile; migration 022 tightens SELECT to MAINTAINER-only
-- (data-quality review is PC's job; Dea never sees the audit machinery).
-- =============================================================
BEGIN;

CREATE TABLE IF NOT EXISTS public.query_audit (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  profile_id      uuid NOT NULL REFERENCES public.profiles(id),
  ts              timestamptz NOT NULL DEFAULT now(),
  intent          text,                 -- the user/skill intent behind the query
  sql             text NOT NULL,         -- the exact SQL executed (read-only, guard-validated)
  plan            text,                  -- EXPLAIN plan text
  quality_verdict jsonb,                 -- {ok, flags:[...], notes} from the quality layer
  row_count       integer,
  flagged         boolean NOT NULL DEFAULT false
);

CREATE INDEX IF NOT EXISTS idx_query_audit_profile_ts
  ON public.query_audit(profile_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_query_audit_flagged
  ON public.query_audit(flagged) WHERE flagged;

GRANT INSERT, SELECT ON public.query_audit TO authenticated;

ALTER TABLE public.query_audit ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS query_audit_profile_access ON public.query_audit;
CREATE POLICY query_audit_profile_access ON public.query_audit
  USING (has_profile_access(profile_id))
  WITH CHECK (has_profile_access(profile_id));

DO $$ BEGIN RAISE NOTICE '021: query_audit created (RLS by profile; 022 tightens SELECT to maintainer)'; END $$;

COMMIT;
