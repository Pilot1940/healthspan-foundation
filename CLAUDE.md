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
- **Messaging**: Telegram Bot
- **Storage**: AWS S3
- **Email**: AWS SES
- **Cache**: Redis
- **Language**: Python

## Current State (2026-06-04)

- **Migrations HEAD: 025 applied.** 026 authored as `*.PROPOSED` (drop 8 dead SaaS-fossil
  tables) — NOT applied, awaiting PC sign-off. No `schema_migrations` table: applied DB
  state is the source of truth; apply via `python scripts/hs_ops.py apply <file>`.
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
