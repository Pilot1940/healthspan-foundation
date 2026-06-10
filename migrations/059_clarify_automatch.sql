-- =============================================================
-- 059_clarify_automatch.sql
-- BACKLOG #15 resolution: clarify answered by a FRESH message, not a reply.
--
-- Users (especially a 14-year-old) answer the bot's "what's unclear — reply
-- to fix it" question by just typing a new message instead of using a
-- Telegram reply. The answer logs fine independently, but the staged item
-- strands as a phantom in the maintainer review queue (5 occurrences by
-- 2026-06-10, all retired by hand).
--
-- Two drain-side layers ship with this migration (monitor/inbox_drain.py):
--   Layer 1 — absorb_pending_clarify: a fresh TEXT message arriving within
--     clarify.match_window_sec of a pending clarify for the same chat is
--     LLM-judged; on a confident match it is processed AS the clarification
--     and the stranded item retires (what a reply would have done).
--   Layer 2 — sweep_staged_orphans: end-of-run safety net; a staged item
--     whose profile logged a matching prod row within
--     clarify.orphan_supersede_window_sec retires as superseded.
--
-- This migration provides:
--   1. The three clarify.* thresholds in system_config (rule #1 — never
--      hardcode; the code keeps matching fallbacks as a safety net).
--   2. Maintainer UPDATE policies on the five stg_*_review tables. The
--      tables had SELECT (maintainer) + INSERT (owner) only — NO UPDATE
--      policy, so no authenticated session could retire a review row (the
--      webhook does it as service_role; PC via the postgres role). The
--      drain runs as the drainer, which passes is_maintainer() (it holds a
--      family_memberships row to PC's maintainer profile — the same
--      mechanism that authorises its maintainer_ingest_* calls), so a
--      maintainer-gated UPDATE policy lets the drain retire review rows.
--      `authenticated` already holds table-level UPDATE (mig 010 revoked
--      only DELETE/TRUNCATE); non-maintainers (Dea) still cannot UPDATE.
--
-- Apply: python3 scripts/hs_ops.py apply migrations/059_clarify_automatch.sql
-- =============================================================
BEGIN;

-- ── 1. clarify.* thresholds ──

INSERT INTO public.system_config (key, value, description, category, is_active)
VALUES
  ('clarify.match_window_sec', '900'::jsonb,
   'Window (seconds) after a clarify question within which a fresh text message from the same chat is LLM-judged as a candidate answer (Layer 1).',
   'clarify', true),
  ('clarify.match_min_conf', '0.7'::jsonb,
   'Minimum LLM confidence for clarify auto-match (Layer 1) and orphan-sweep supersede (Layer 2). Below this, the message processes independently / the orphan stays.',
   'clarify', true),
  ('clarify.orphan_supersede_window_sec', '1800'::jsonb,
   'Window (seconds) after staging within which an independently-logged prod row can supersede a staged orphan (Layer 2 sweep).',
   'clarify', true)
ON CONFLICT (key) DO UPDATE
  SET description = EXCLUDED.description,
      is_active = true,
      updated_at = now();

-- ── 2. Maintainer UPDATE policies on stg_*_review (retire review rows) ──

DROP POLICY IF EXISTS stg_food_log_review_upd ON public.stg_food_log_review;
CREATE POLICY stg_food_log_review_upd ON public.stg_food_log_review
  FOR UPDATE USING (is_maintainer()) WITH CHECK (is_maintainer());

DROP POLICY IF EXISTS stg_supplement_intake_review_upd ON public.stg_supplement_intake_review;
CREATE POLICY stg_supplement_intake_review_upd ON public.stg_supplement_intake_review
  FOR UPDATE USING (is_maintainer()) WITH CHECK (is_maintainer());

DROP POLICY IF EXISTS stg_biomarker_review_upd ON public.stg_biomarker_review;
CREATE POLICY stg_biomarker_review_upd ON public.stg_biomarker_review
  FOR UPDATE USING (is_maintainer()) WITH CHECK (is_maintainer());

DROP POLICY IF EXISTS stg_food_rule_review_upd ON public.stg_food_rule_review;
CREATE POLICY stg_food_rule_review_upd ON public.stg_food_rule_review
  FOR UPDATE USING (is_maintainer()) WITH CHECK (is_maintainer());

DROP POLICY IF EXISTS stg_test_result_review_upd ON public.stg_test_result_review;
CREATE POLICY stg_test_result_review_upd ON public.stg_test_result_review
  FOR UPDATE USING (is_maintainer()) WITH CHECK (is_maintainer());

-- ── verify ──

DO $$
DECLARE
  v_keys int;
  v_pols int;
BEGIN
  SELECT count(*) INTO v_keys FROM public.system_config
  WHERE key IN ('clarify.match_window_sec','clarify.match_min_conf',
                'clarify.orphan_supersede_window_sec') AND is_active;
  IF v_keys <> 3 THEN
    RAISE EXCEPTION 'Migration 059 verify: expected 3 clarify.* keys, found %', v_keys;
  END IF;

  SELECT count(*) INTO v_pols FROM pg_policies
  WHERE schemaname = 'public'
    AND tablename IN ('stg_food_log_review','stg_supplement_intake_review',
                      'stg_biomarker_review','stg_food_rule_review','stg_test_result_review')
    AND cmd = 'UPDATE' AND qual = 'is_maintainer()';
  IF v_pols <> 5 THEN
    RAISE EXCEPTION 'Migration 059 verify: expected 5 maintainer UPDATE policies, found %', v_pols;
  END IF;

  RAISE NOTICE 'Migration 059 verify: OK';
END $$;

COMMIT;
