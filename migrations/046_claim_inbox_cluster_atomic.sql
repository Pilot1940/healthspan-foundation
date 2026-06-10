-- Migration 046: atomic cluster claim RPC for inbox_drain
--
-- Problem: inbox_drain.py claimed multi-row clusters row-by-row in a Python loop.
-- Between two concurrent drains, Drain A could claim rows [r1,r2] and Drain B
-- claims [r3] of the same cluster before Drain A gets to r3. Drain B would then
-- process [r3] as a singleton — a partial-cluster write. No double-processing
-- occurs (the rollback on skipped=True is correct) but the cluster is fragmented.
--
-- Fix: one SQL function that claims ALL rows or leaves ALL rows pending.
-- Uses FOR UPDATE to serialise concurrent callers, then checks every id is still
-- pending before applying the UPDATE.  Returns TRUE iff all rows were claimed.
--
-- The function is SECURITY INVOKER — runs under the caller's JWT (healthspan.drainer),
-- respecting the existing media_inbox RLS, the same posture as the PATCH-based
-- claim_inbox_item.  No maintainer guard — the drainer is not a maintainer.
--
-- Apply: python3 scripts/hs_ops.py apply migrations/046_claim_inbox_cluster_atomic.sql

CREATE OR REPLACE FUNCTION public.claim_inbox_cluster(p_item_ids uuid[])
RETURNS boolean
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public
AS $$
DECLARE
  v_pending_count int;
BEGIN
  -- Lock the target rows to serialise concurrent callers for this cluster.
  -- SKIP LOCKED is intentionally NOT used: we want to wait, not silently skip.
  PERFORM 1
  FROM public.media_inbox
  WHERE id = ANY(p_item_ids)
  FOR UPDATE;

  -- All rows must still be pending; if any was claimed by a concurrent drain,
  -- abort without touching anything.
  SELECT COUNT(*) INTO v_pending_count
  FROM public.media_inbox
  WHERE id = ANY(p_item_ids)
    AND status = 'pending';

  IF v_pending_count <> array_length(p_item_ids, 1) THEN
    RETURN false;
  END IF;

  -- Atomically claim the whole cluster.
  UPDATE public.media_inbox
  SET status = 'processing'
  WHERE id = ANY(p_item_ids);

  RETURN true;
END;
$$;

REVOKE ALL ON FUNCTION public.claim_inbox_cluster(uuid[]) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.claim_inbox_cluster(uuid[]) FROM anon;
GRANT EXECUTE ON FUNCTION public.claim_inbox_cluster(uuid[]) TO authenticated;

NOTIFY pgrst, 'reload schema';

-- ── Verify ────────────────────────────────────────────────────────────────────
DO $$
DECLARE
  n int;
BEGIN
  SELECT COUNT(*) INTO n
  FROM pg_proc
  WHERE proname = 'claim_inbox_cluster'
    AND pronamespace = 'public'::regnamespace
    AND pronargs = 1;
  ASSERT n = 1, format('claim_inbox_cluster 1-param count=%s (expected 1)', n);

  RAISE NOTICE 'Migration 046 verify: OK — claim_inbox_cluster created';
END $$;
