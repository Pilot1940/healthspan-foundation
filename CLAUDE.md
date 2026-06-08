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
- **Messaging**: Telegram (bot confirmations + data ingestion channel) + Google Chat (future push notifications — not yet implemented in code)
- **Storage**: AWS S3
- **Email**: AWS SES
- **Cache**: Redis
- **Language**: Python

## Current State (2026-06-08)

- **Migrations 024–028 all applied** (024 drop-backup, 025 query_audit reshape, 026 drop 8
  dead SaaS tables, 027 supplement source vocab +photo, 028 drop 4 more fossils —
  user_telegram_links/locations/log_type_config/source_priority_config; KEPT audit_log +
  canonical_aliases). **54 public tables** now. No `schema_migrations` table: applied DB
  state is the source of truth; apply via `python scripts/hs_ops.py apply <file>`.
- **Migrations 029–042 all written and applied** — Telegram ingestion Phase 1–3B + drain
  identity + completeness gate + stage_reason + supplement/biomarker alignment + taken_on fix +
  write-contract audit. 035 re-keyed drain identity to `healthspan.drainer@chitalkar.com`.
  **036 (applied 2026-06-07)** added `p_force_stage` to `maintainer_ingest_food` + DB-side kcal gate.
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
  **040 (applied 2026-06-07)** write-contract audit: added `'telegram'` to
  `supplement_intake_logs.source_check`; DB-side NOT NULL guards in supplement
  (supplement_id), biomarker (metric_definition_id, value), and food (description,
  meal_type CHECK) RPCs; fixed supplement RETURN to emit `v_stage_reason` not
  `p_stage_reason`; removed `'unknown'` from food vision prompt meal_type options.
  **041 (applied 2026-06-07)** smart food resolution: `food_reference` table (shared macro
  library; global rows have `profile_id IS NULL`, personal rows scoped to user); per-user
  `food_guidance` via nullable `profile_id` column + `effective_food_guidance` view updated;
  SECURITY DEFINER RPCs `lookup_food_reference`, `lookup_viome_verdicts`,
  `promote_food_to_reference`; `food_reference.learn_min_logs` threshold in
  `system_config`; Hooray Strawberry Shake seed row (global).
  **042 (applied 2026-06-07)** pg_net trigger-drain webhook: `fn_media_inbox_notify`
  SECURITY DEFINER function + `trg_media_inbox_notify` AFTER INSERT trigger on
  `media_inbox` — fires `net.http_post` to `trigger-drain` Edge function for every new
  `pending` row; deduplication handled by the Edge function.
  **043 (applied 2026-06-08)** seed `trigger_drain.last_dispatch_ts` in `system_config`
  for atomic CAS dedup; updated `fn_media_inbox_notify` to forward `TRIGGER_DRAIN_SECRET`
  from Supabase vault. ⚠️ Set `TRIGGER_DRAIN_SECRET` via `supabase secrets set` and
  `vault.create_secret`, then redeploy trigger-drain before this has effect.
  **044 (applied 2026-06-08)** dropped stale `food_guidance_item_classification_scope_key`
  UNIQUE constraint (from mig 004); added `food_guidance_global_item_class` partial index
  for global rows (`profile_id IS NULL`) to support multi-profile Viome guidance.
  **045 (applied 2026-06-08)** WHOOP full schema expansion: added all missing WHOOP v2 API
  fields to `whoop_cycles` (score_state, recovery_score_state, recovery_user_calibrating,
  sleep_cycle_count, disturbance_count, no_data_min, whoop_updated_at/created_at),
  `whoop_sleeps` (score_state, no_data_min, sleep_cycle_count, disturbance_count,
  whoop_cycle_id, whoop_updated_at/created_at), `whoop_workouts` (score_state, sport_id,
  percent_recorded, distance_m, altitude_gain_m, altitude_change_m, whoop_updated_at/
  created_at). New table `whoop_body_measurements` (profile_id, synced_date, height_m,
  weight_kg, max_heart_rate) for `GET /v2/user/measurement/body` — UNIQUE (profile_id,
  synced_date). Backfilled: 519 workouts, 580 cycles, 625 sleeps for PC (2020-01-01→now).
  **046 (applied 2026-06-08)** `claim_inbox_cluster(uuid[])` RPC — atomic cluster claim
  using `FOR UPDATE` lock + all-or-nothing UPDATE; SECURITY INVOKER, granted to authenticated.
- **Drain service account**: `healthspan.drainer@chitalkar.com` (HS_AUTH_EMAIL env var).
  UID resolved dynamically in migration 035 — no hardcoded UUID.
- **Event-driven drain pipeline**: Telegram photo → `media_inbox` INSERT → pg_net trigger
  `fn_media_inbox_notify` → `trigger-drain` Edge function (15s dedup via system_config CAS)
  → GitHub `repository_dispatch` `inbox-drain` → GH Actions → `monitor/inbox_drain.py --settle-sec 5`.
  Text messages → `telegram-webhook` → GH `repository_dispatch` `send-brief` → `monitor/brief.py`.
- **WHOOP sync**: `ingest/whoop_sync.py` — `sync_profile()` runs workouts + cycles + sleeps +
  body_measurements for each profile. `--backfill` from 2020-01-01. `refresh_recent()` for
  brief pre-sync. Score state tracked: SCORED / PENDING_SCORE / UNSCORABLE — brief shows ⏳
  for pending, ❌ for unscorable. WHOOP body endpoint: height_m, weight_kg, max_heart_rate.
  ⚠️ whoop-webhook needs redeploy: `supabase functions deploy whoop-webhook --no-verify-jwt`.
- **`lib/contract.write` skips GENERATED columns** on INSERT/UPDATE while keeping them
  as `ON CONFLICT` targets. `supplement_intake_logs.taken_on` IS `GENERATED ALWAYS AS
  ((taken_at AT TIME ZONE 'UTC')::date)` — confirmed on live DB. Migration 039 removed
  it from the `maintainer_ingest_supplement` INSERT (was causing error 428C9); it is
  still used as the ON CONFLICT target, auto-populated by Postgres from `taken_at`.
- **67 tables** (36 active / 31 empty), 7 views. Full inventory +
  dormant-table classification: `docs/HEALTH-CHECK-2026-06-03.md`.
- **Maintainer model** (022/023): PC is the sole maintainer (`profiles.is_maintainer`,
  resolved via `family_memberships`). Maintainer-only RLS SELECT on `query_audit`,
  `wearable_sync_log`/`_errors`, `stg_*_review`. Dea sees outcomes, never the machinery.
- **Query logging** (025): every skill read lands in `query_audit` —
  `lib/views.run_view` (catalog) + `lib/sql_guard.run_adhoc_audited` (adhoc) via
  non-blocking `log_query`. Maintainer digest: `monitor/query_log.py`.
- v3 modules: `lib/` (context, views, sql_guard, db, contract), `ingest/`, `plan/`,
  `monitor/`, `analysis/`, `export/`. SCHEMA-MAP regenerated via `scripts/gen_schema_map.py`.
