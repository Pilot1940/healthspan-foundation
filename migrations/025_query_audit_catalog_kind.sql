-- =============================================================
-- 025_query_audit_catalog_kind.sql  (RESHAPE — make query_audit log EVERY skill query)
-- 021 created query_audit for AD-HOC queries only (intent/sql/plan). V3 widens it to log
-- EVERY skill read for maintainer observability:
--   * kind='catalog' — a named lib/views run: name_or_sql=view name, params=view params.
--   * kind='adhoc'   — a guard-validated ad-hoc SELECT: name_or_sql=the SQL,
--                      params={"intent": ...} (the ad-hoc `intent` now folds into params,
--                      since the canonical column set drops the standalone `intent` col),
--                      quality_verdict=the quality-layer verdict.
--
-- Canonical columns: kind, name_or_sql, params, row_count, quality_verdict, flagged, ts,
-- profile_id. The verbose `plan` (EXPLAIN text) is dropped — observability needs the
-- shape of the query and its verdict, not the planner output.
--
-- query_audit is EMPTY today (0 rows), so this reshape needs no backfill; a guard aborts
-- if that ever stops being true. RLS is unchanged: maintainer-only SELECT, owner-scoped
-- INSERT (migration 022) — Dea never sees the audit trail.
-- =============================================================
BEGIN;

-- Guard: this reshape assumes an empty table (no backfill path for the new NOT NULLs).
DO $$
DECLARE n int;
BEGIN
  SELECT count(*) INTO n FROM public.query_audit;
  IF n > 0 THEN
    RAISE EXCEPTION '025: expected empty query_audit, found % row(s) — add a backfill first', n;
  END IF;
END $$;

-- New canonical columns.
ALTER TABLE public.query_audit ADD COLUMN IF NOT EXISTS kind        text;
ALTER TABLE public.query_audit ADD COLUMN IF NOT EXISTS name_or_sql text;
ALTER TABLE public.query_audit ADD COLUMN IF NOT EXISTS params      jsonb;

ALTER TABLE public.query_audit ALTER COLUMN kind        SET NOT NULL;
ALTER TABLE public.query_audit ALTER COLUMN name_or_sql SET NOT NULL;

ALTER TABLE public.query_audit DROP CONSTRAINT IF EXISTS query_audit_kind_chk;
ALTER TABLE public.query_audit
  ADD CONSTRAINT query_audit_kind_chk CHECK (kind IN ('catalog', 'adhoc'));

-- Drop superseded columns: intent folds into params; plan (EXPLAIN text) is not needed
-- for observability; sql is renamed in spirit to name_or_sql.
ALTER TABLE public.query_audit DROP COLUMN IF EXISTS intent;
ALTER TABLE public.query_audit DROP COLUMN IF EXISTS sql;
ALTER TABLE public.query_audit DROP COLUMN IF EXISTS plan;

-- Index for the monitor's "recent by kind" session-start summary.
CREATE INDEX IF NOT EXISTS idx_query_audit_kind_ts ON public.query_audit(kind, ts DESC);

DO $$ BEGIN
  RAISE NOTICE '025: query_audit reshaped (kind/name_or_sql/params; intent->params; dropped sql/plan)';
END $$;

COMMIT;
