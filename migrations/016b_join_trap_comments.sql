-- =============================================================
-- 016b_join_trap_comments.sql  (ADDITIVE — COMMENT ON only)
-- Surfaced by Case-2 dry-run: whoop_journal.cycle_start (old CSV source, e.g.
-- 2024-10-25 22:49:31) and whoop_cycles.cycle_start (API, e.g. ...490000) DIFFER
-- at the timestamp level and DO NOT join exactly — they only match on ::date.
-- An exact-timestamp join silently returns 0 rows (a confidently-wrong "no data"
-- answer that EXPLAIN cannot catch). Encode the join rule in the comments.
-- =============================================================
BEGIN;

COMMENT ON COLUMN public.whoop_cycles.cycle_start IS
  'Start of the WHOOP physiological day (timestamptz, UTC). TRAP: this is the WHOOP cycle boundary = screenshot date MINUS 1 day; not the calendar date. JOIN TRAP: to join whoop_journal (CSV-sourced timestamps) join on cycle_start::date, NOT the exact timestamp — exact match returns 0 rows.';

COMMENT ON VIEW public.whoop_journal IS
  'WHOOP daily journal (security_invoker pivot view over whoop_journal_entries): 27 boolean habit columns per cycle incl. had_alcohol. JOIN TRAP: its cycle_start comes from the original CSV and does NOT exactly match whoop_cycles.cycle_start (API) — always join on cycle_start::date.';

COMMENT ON COLUMN public.whoop_journal.had_alcohol IS
  'TRUE = alcohol logged that cycle. Join to whoop_cycles on cycle_start::date (not exact timestamp).';

DO $$ BEGIN RAISE NOTICE '016b: cross-source date-join trap encoded on whoop_cycles.cycle_start + whoop_journal.'; END $$;
COMMIT;
