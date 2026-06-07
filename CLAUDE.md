# Healthspan Foundation — Claude Rules

## Non-Negotiable Rules

1. **Never hardcode thresholds** — all thresholds must come from the `system_config` table.
2. **AI extraction output goes to staging first** — all AI extraction output must be written to `stg_*` staging tables, never directly to production tables.
3. **Every new table needs an RLS policy** — the RLS policy must be included in the same migration that creates the table.
4. **Test files mirror source structure** — `src/foo.py` → `tests/unit/test_foo.py`.
5. **Commit format** — use conventional prefixes: `feat:` | `fix:` | `schema:` | `test:` | `docs:` | `chore:` | `adr:`
6. **Never modify auth logic** without explicit instruction from the user.
7. **When stuck for more than 2 attempts**, stop and ask the user for guidance.

## Stack

- **Database**: Supabase (PostgreSQL + Row Level Security)
- **AI**: Anthropic Claude API
- **Messaging**: Google Chat (push via webhook URLs in Supabase secrets)
- **Storage**: AWS S3
- **Email**: AWS SES
- **Cache**: Redis
- **Language**: Python

## Current State (2026-06-07)

- **Migrations 024–028 all applied** (024 drop-backup, 025 query_audit reshape, 026 drop 8
  dead SaaS tables, 027 supplement source vocab +photo, 028 drop 4 more fossils —
  user_telegram_links/locations/log_type_config/source_priority_config; KEPT audit_log +
  canonical_aliases). **54 public tables** now. No `schema_migrations` table: applied DB
  state is the source of truth; apply via `python scripts/hs_ops.py apply <file>`.
- **Migrations 029–039 all written and applied** — Telegram ingestion Phase 1–3B + drain
  identity + completeness gate + stage_reason + supplement/biomarker alignment + taken_on fix. 035
  re-keyed drain identity to `healthspan.drainer@chitalkar.com`. **036 (applied
  2026-06-07)** added `p_force_stage` to `maintainer_ingest_food` + DB-side kcal gate.
  **037 (applied 2026-06-07)** added `stage_reason` to `media_inbox` +
  `stg_food_log_review`. **038 (applied 2026-06-07)** rebuilt
  `maintainer_ingest_supplement` + `maintainer_ingest_biomarker` to mirror food pattern:
  `p_force_stage boolean DEFAULT FALSE` + `p_stage_reason text DEFAULT NULL`, same
  routing/guard/grants; added `stage_reason` to `stg_biomarker_review` +
  `stg_supplement_intake_review`; biomarker has DB-side `plausible_min/max` gate;
  drain now uses per-kind completeness gates (`supplement_is_complete`,
  `biomarker_is_complete`) + improved `unknown` prompt with per-kind schemas.
  **039 (applied 2026-06-07)** fixed `maintainer_ingest_supplement` — removed `taken_on`
  from INSERT (GENERATED column, 428C9 error); kept in ON CONFLICT target.
- **Drain service account**: `healthspan.drainer@chitalkar.com` (HS_AUTH_EMAIL env var).
  UID resolved dynamically in migration 035 — no hardcoded UUID.
- **WHOOP strain refresh (v3-8):** cycles go stale at ~0 strain (no `cycle.updated`
  webhook). `ingest/whoop_sync.refresh_recent()` is the reusable mini-sync; the
  `whoop-webhook` refreshes the prior cycle on `sleep.*`. ⚠️ webhook needs redeploy:
  `supabase functions deploy whoop-webhook --no-verify-jwt`.
- **`lib/contract.write` skips GENERATED columns** on INSERT/UPDATE while keeping them
  as `ON CONFLICT` targets. `supplement_intake_logs.taken_on` IS `GENERATED ALWAYS AS
  ((taken_at AT TIME ZONE 'UTC')::date)` — confirmed on live DB. Migration 039 removed
  it from the `maintainer_ingest_supplement` INSERT (was causing error 428C9); it is
  still used as the ON CONFLICT target, auto-populated by Postgres from `taken_at`.
- **66 tables** (35 active / 31 empty), 7 views, 126 FKs (0 orphans). Full inventory +
  dormant-table classification: `docs/HEALTH-CHECK-2026-06-03.md`.
- **Maintainer model** (022/023): PC is the sole maintainer (`profiles.is_maintainer`,
  resolved via `family_memberships`). Maintainer-only RLS SELECT on `query_audit`,
  `wearable_sync_log`/`_errors`, `stg_*_review`. Dea sees outcomes, never the machinery.
- **Query logging** (025): every skill read lands in `query_audit` —
  `lib/views.run_view` (catalog) + `lib/sql_guard.run_adhoc_audited` (adhoc) via
  non-blocking `log_query`. Maintainer digest: `monitor/query_log.py`.
- v3 modules: `lib/` (context, views, sql_guard, db, contract), `ingest/`, `plan/`,
  `monitor/`, `analysis/`, `export/`. SCHEMA-MAP regenerated via `scripts/gen_schema_map.py`.
