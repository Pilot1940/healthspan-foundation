# HealthSpan Skill — Changelog

## v3 — cleanup & observability pass (2026-06-04)
- **Health check** (`docs/HEALTH-CHECK-2026-06-03.md`): full live inventory — 66 tables
  (35 active / 31 empty), 126 FKs with **0 orphans**, 0 invalid indexes, 0 `NOT VALID`
  constraints, 100% RLS on profile_id tables. `whoop_tokens` confirmed intentionally
  default-deny (service_role-only credential vault).
- **Migration 024**: dropped stray `food_logs_dedup_backup_20260602` (no FK refs, dumps exist).
- **Migration 025**: `query_audit` reshaped to log **every** skill query — columns
  `kind`/`name_or_sql`/`params`/`row_count`/`quality_verdict`/`flagged`/`ts`/`profile_id`
  (`intent`→`params`, dropped `sql`/`plan`). `run_view` logs `kind='catalog'`;
  `run_adhoc_audited` logs `kind='adhoc'`+verdict via `lib.sql_guard.log_query`
  (savepoint-wrapped — audit failure never breaks the read path). Maintainer-only RLS intact.
- **`monitor/query_log.py`**: maintainer session-start query digest (counts, top views, flagged).
- **Migration 026 (PROPOSED, not applied)**: drop 8 dead SaaS-fossil tables
  (`user_invites`/`user_invite_tokens`/`user_tier_history`/`user_locations`/
  `user_log_type_prefs`/`user_preference_history`/`brain_conversations`/`brain_messages`) —
  dry-run-validated, awaiting PC sign-off.

## v3 — context-driven modular skill (2026-06-03)
The v2 engine (ingestion + contract + guards + views + emitters + Edge + RLS + 5 safety
layers) stands; v3 makes it a great modular skill. Every module is standalone-invokable.

- **Context-MD layer** (`lib/context.py` + `context/*.context.md`): per-person targets/norms/
  coaching-voice/safety. The skill reads ALL targets from here — never a population default;
  a missing entry → ask. Minor-safety (Dea) lives in her context file, not a code branch.
- **Migrations 020/021/022/023**: multiple concurrent goals (dropped the one-active uniqueness);
  `query_audit`; maintainer model (`is_maintainer()` via family_memberships; maintainer-only
  RLS SELECT on `query_audit`/`wearable_sync_log`/`wearable_sync_errors`/`stg_*_review` — a
  non-maintainer sees 0 rows, ingestion writes still work via client-UUIDs + the
  `hs_close_sync_log` definer).
- **plan/**: `training_plan.py`, `goals.py` (multiple concurrent, direction-aware progress).
- **monitor/**: `trend_monitor.py` (recovery/HRV/strap/biomarker/goal shifts, self-calibrating),
  `ingest_health.py` (maintainer-only data-quality surfacing).
- **analysis/**: `trends.py` (sleep/workout narratives, NULL-recovery aware),
  `supplement_summary.py` (regimens + adherence), `food_chat.py` (conversational diet log:
  preview → confirm → write, running total vs the context-MD calorie target).
- **sql_guard quality layer**: ad-hoc queries get a quality pass (fan-out, aggregate-over-NULL,
  scoping, empty-result) and are logged to `query_audit`. Catalog views skip it.
- **SKILL.md v3**: session-start sequence (config → context MD + notes → ingest_health[maintainer]
  → trend_monitor → greet), intent→route map, maintainer model, minor-safety, cite/quality guards.

Implausibility bound (017–019, prior): physiological `plausible_min/max` gate OCR/unit/comma
slips to staging on all in-use metrics; clinical reference ranges still flag-not-gate.

## v2 — DB-backed engine
Ingestion contract, vision OCR, lib/views catalog + emitters, Supabase Edge (WHOOP OAuth +
webhook), RLS access model A, schema-map column COMMENTs, SQL/cite guards.
