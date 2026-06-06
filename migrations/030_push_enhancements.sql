-- =============================================================
-- 030_push_enhancements.sql  (FEATURE — push_log.dedup_value + system_config push thresholds)
-- 2026-06-07
--
-- Context: Phase 2 push infrastructure (spec §3 debounce + §7 dead-man).
-- push_log (029) stored no metric value at push time, making "materially changed"
-- re-score suppression impossible to evaluate without the column.
--
-- Changes:
--   1. push_log.dedup_value numeric — the metric score at push time
--      (recovery_score_pct for recovery pushes, activity_strain for workouts).
--      NULL for non-numeric push types (dead_man, media_done).
--   2. system_config seeds — push.* + push.reconcile.* thresholds.
--      CLAUDE.md rule #1: no hardcoded thresholds; all live here.
--
-- Auth: ALTER is DDL (service_role only). Edge fn reads config via SELECT on
-- system_config (already readable by authenticated via existing RLS).
-- =============================================================

BEGIN;

-- ── push_log.dedup_value ─────────────────────────────────────────────────────
ALTER TABLE public.push_log ADD COLUMN IF NOT EXISTS dedup_value numeric;

COMMENT ON COLUMN public.push_log.dedup_value IS
  'Metric value at push time (recovery_score_pct for recovery pushes, activity_strain '
  'for workouts). Used by the debounce/material-change check: a re-score within the '
  'debounce window only re-pushes if ABS(current − dedup_value) >= push.material_change_pct. '
  'NULL for non-numeric push types (dead_man, media_done). TRAP: the DB upsert has '
  'already overwritten the whoop row by the time the check runs — read dedup_value '
  'from the most-recent push_log row for (profile, type, subject), not from the live table.';

-- ── system_config seeds ───────────────────────────────────────────────────────
-- ON CONFLICT DO NOTHING → idempotent on re-run; change values via UPDATE, not here.
INSERT INTO public.system_config (key, value, description, category, is_active) VALUES
  (
    'push.debounce_window_min', '30'::jsonb,
    'Debounce window (minutes). A burst of (profile, push_type, subject_id) events '
    'within this window collapses to one push, unless the score changes materially '
    '(see push.material_change_pct). Critical alerts bypass this.',
    'push', true
  ),
  (
    'push.quiet_hours_start', '22'::jsonb,
    'Quiet-hours start hour (local time, 0–23). Non-critical pushes are suppressed '
    'from this hour. Evaluated in the profile''s WHOOP timezone (cyc.timezone_offset). '
    'Wraps midnight when start > end (e.g. 22–07).',
    'push', true
  ),
  (
    'push.quiet_hours_end', '7'::jsonb,
    'Quiet-hours end hour (local time, 0–23). Non-critical pushes resume at this hour.',
    'push', true
  ),
  (
    'push.recovery_critical_threshold', '34'::jsonb,
    'Recovery score (%) below which a recovery_landed push is classified CRITICAL — '
    'sent immediately regardless of quiet hours or debounce window.',
    'push', true
  ),
  (
    'push.hrv_crash_pct', '20'::jsonb,
    'HRV drop (%) vs recent 7-cycle baseline that triggers a CRITICAL alert. '
    'Example: avg HRV 60 ms, crash_pct 20 → < 48 ms = critical.',
    'push', true
  ),
  (
    'push.material_change_pct', '5'::jsonb,
    'Minimum score change (percentage points) for a re-score within the debounce window '
    'to be re-pushed. Prevents re-score spam while surfacing meaningful updates.',
    'push', true
  ),
  (
    'push.dead_man_hours', '36'::jsonb,
    'Hours of no WHOOP data (records_upserted > 0 in wearable_sync_log) before a '
    'dead-man alert is pushed to PC. Threshold measured from last successful data row, '
    'not from last sync run (empty syncs do not reset the clock).',
    'push', true
  ),
  (
    'push.reconcile_default_hours', '48'::jsonb,
    'Default WHOOP reconcile window (hours) used by monitor.reconcile. Widens '
    'per-profile if that profile''s last successful sync is older.',
    'push', true
  ),
  (
    'push.reconcile_max_widen_hours', '168'::jsonb,
    'Maximum per-profile reconcile window (hours = 7 days). Caps the age-aware widen '
    'so reconcile never triggers an unbounded backfill on a long-dormant profile.',
    'push', true
  )
ON CONFLICT (key) DO NOTHING;

-- ── Verify ────────────────────────────────────────────────────────────────────
DO $$
DECLARE n int; c int;
BEGIN
  SELECT count(*) INTO n FROM information_schema.columns
  WHERE table_schema='public' AND table_name='push_log' AND column_name='dedup_value';
  IF n = 0 THEN
    RAISE EXCEPTION '030: push_log.dedup_value column not found';
  END IF;

  SELECT count(*) INTO c FROM public.system_config
  WHERE key LIKE 'push.%' AND is_active;
  IF c < 9 THEN
    RAISE EXCEPTION '030: expected 9 push.* system_config rows, found %', c;
  END IF;

  RAISE NOTICE '030 OK — push_log.dedup_value added; % push.* config rows active.', c;
END $$;

COMMIT;
