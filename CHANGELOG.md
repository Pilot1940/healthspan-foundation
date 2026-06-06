# HealthSpan Skill — Changelog

## v3.4.0 — Telegram ingestion Phase 2: reconcile + push + dead-man (2026-06-07)

- **Migration 030 — push_log.dedup_value + system_config push thresholds.**
  - `push_log.dedup_value numeric` — stores the metric score at push time (recovery_score_pct
    for recovery pushes, activity_strain for workouts). Enables "materially changed" re-score
    suppression within the debounce window: re-push only if change ≥ push.material_change_pct.
  - 9 new `push.*` system_config rows seeded (rule #1 — no hardcoded thresholds):
    `debounce_window_min=30`, `quiet_hours_start=22`, `quiet_hours_end=7`,
    `recovery_critical_threshold=34`, `hrv_crash_pct=20`, `material_change_pct=5`,
    `dead_man_hours=36`, `reconcile_default_hours=48`, `reconcile_max_widen_hours=168`.
  - Verify: `push_log.dedup_value` present; all 9 `push.*` rows active.
- **`monitor/reconcile.py` — age-aware WHOOP reconcile (importable + CLI).**
  - Importable: `from monitor.reconcile import reconcile` — called before morning digest.
  - CLI: `python -m monitor.reconcile [--profile PC] [--hours 24]`.
  - Per-profile age-aware window: reads `push.reconcile_default_hours` (48h default) from
    system_config; widens per profile if last successful `wearable_sync_log` run is older
    (`max(default, ceil(hours_since_last) + 2h buffer)`); caps at `push.reconcile_max_widen_hours`
    (168h / 7d) to prevent unbounded backfill on long-dormant profiles.
  - Never-synced profile gets `max_widen_hours` (safe initial window).
  - Per-profile error isolation — one dead token never blocks others.
  - Returns `{profile_id: {"window_hours": int, "results": list | dict}}`.
- **`supabase/functions/whoop-webhook/index.ts` — Phase 2 push additions.**
  - **Short WHOOP pushes (best-effort, non-blocking).** After each upsert, the webhook
    composes and sends a short templated push via the Telegram bot:
    - `recovery_landed` push (recovery/cycle events): includes recovery_score_pct, hrv_ms,
      resting_hr_bpm. Minor framing for `is_minor=true` (growth/performance language).
    - `workout_logged` push (workout events): includes activity_name, strain, duration_min.
    - All thresholds from system_config; no hardcoded values.
  - **Debounce**: `(profile_id, push_type, subject_id)` within `push.debounce_window_min`
    (30min) collapses to one push. Re-score within window re-pushes only if score changes
    ≥ `push.material_change_pct` (5ppt) — requires `push_log.dedup_value`.
  - **Quiet hours**: non-critical pushes suppressed between `push.quiet_hours_start` (22)
    and `push.quiet_hours_end` (7). Evaluated in the recipient's **local timezone**
    (WHOOP `timezone_offset` field), not UTC. Handles both IANA names and UTC offset strings.
  - **Critical alerts** (recovery < 34% OR HRV crash ≥ 20% vs 7-cycle baseline) bypass
    quiet hours and debounce — always send. HRV crash queries last 7 `whoop_cycles.hrv_ms`
    rows. Altitude early-warning deferred (no confirmed data source in WHOOP schema).
  - **Idempotency**: every push attempt logged to `push_log` (status: `sent` or `suppressed`).
    Webhook replay for the same `(type, id)` within the debounce window → `suppress_debounce`.
  - **Dead-man heartbeat**: on every WHOOP event, checks ALL active profiles for 36h data
    silence (`wearable_sync_log.records_upserted > 0` anchor — empty syncs do not reset
    the clock). Pushes an alert to PC if overdue; suppressed for 22h after each alert.
    Runs best-effort in a detached async closure — never delays the 200 response.
  - **Exported pure functions** (`localHour`, `decidePush`, `composePush`, `deadManThreshold`)
    — no DB/network dependencies, fully unit-testable.
- **Tests — 12 Python unit tests** (`tests/unit/test_reconcile.py`):
  `reconcile widens on stale sync`, `uses default for recent sync`, `caps at max_widen`,
  `never-synced uses max_widen`, `exactly-at-boundary widens by buffer`, `buffer ensures overlap`,
  plus `get_config_int` and `last_sync_hours_ago` helpers. All 12 pass (no live DB needed).
- **Tests — 20 Deno unit tests** (`supabase/functions/whoop-webhook/webhook_test.ts`):
  `critical-always` (quiet + debounce bypassed), `debounce collapses burst to one push`,
  `material-change overrides debounce`, `sub-threshold change stays debounced`,
  `quiet-hours suppression`, `quiet-hours uses local timezone not UTC`, `sends after quiet end`,
  `push idempotency on replay`, `localHour` (IANA / offset-string / null / fallback),
  `deadManThreshold` (null / exact-36h / under-36h / recent), `composePush` (recovery adult +
  minor framing, workout adult + minor, unknown type fallback).
- **Debounce window**: 30 minutes (`push.debounce_window_min`).
- **Dead-man threshold**: 36 hours (`push.dead_man_hours`); trigger = `wearable_sync_log`
  rows with `records_upserted > 0`; suppressed for 22h after each alert.
- **Note — fully-silent scenario**: dead-man check runs on each incoming WHOOP event
  and covers all profiles. If ALL profiles go simultaneously silent (no webhooks), a
  separate pg_cron job calling the function endpoint is required as a backstop (not yet
  deployed — schedule when an availability incident makes it warranted).

## v3.3.0 — Telegram ingestion Phase 1 (2026-06-06)

- **Migration 029 — Telegram ingestion tables + RLS.** Reinstates Telegram as an
  ingestion channel (029 supersedes the `user_telegram_links` fossil dropped in 028).
  Five new tables, all with RLS and 016-style column comments:
  - `telegram_link_codes` — one-time codes PC mints before a chat_id is known. Maintainer-only
    RLS; code is a credential, single-use, 7-day expiry. Minted via `hs_ops.py mint-link-code`.
  - `telegram_identities` — chat_id → profile_id binding. Status: pending → active → revoked.
    RLS: a profile sees its own row; maintainer sees all. Only service role / maintainer writes.
  - `telegram_processed_updates` — inbound update_id idempotency latch. Written ONLY after
    media_inbox insert succeeds (ordering rule — Telegram retry safety).
  - `media_inbox` — photo/text queue drained by Phase-3 Routine. kind guessed from caption
    keywords (food/workout/lab/dexa/unknown). caption stored as DATA only (injection rule).
  - `push_log` — outbound push idempotency + debounce ledger. Debounce key:
    (profile_id, push_type, subject_id).
  - All 5 tables: DELETE/TRUNCATE revoked from `authenticated` + `healthspan_app`; ALL
    revoked from `anon`. SCHEMA-MAP coverage 54 → 59/59.
- **Edge fn `telegram-webhook` (Deno).** Deployed to `dsnydskkjwziynwmzfkh`. Reads secrets:
  `TELEGRAM_BOT_TOKEN`, `TELEGRAM_WEBHOOK_SECRET`, `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`.
  - Secret-token verification: constant-time comparison of `X-Telegram-Bot-Api-Secret-Token`.
  - Idempotency: checks `telegram_processed_updates` on entry; writes update_id ONLY after
    successful `media_inbox` insert (so Telegram retry re-runs on any earlier failure).
  - Identity: chat_id → `telegram_identities`. Unknown/pending → onboarding prompt + maintainer
    alert; revoked → rejection. Active → resolve profile_id.
  - Link-code activation: `/start <code>` or bare code from unknown/pending chat → validates
    against `telegram_link_codes` (unexpired, unused) → flips identity to active.
  - Photo/document: `getFile` + download → upload to `health-media` private bucket (signed
    URLs only) → `media_inbox` row (status=pending). Download failure is non-fatal (null
    storage_path; Routine re-fetches).
  - Text: `media_inbox` row with caption. Caption is DATA only — injection rule enforced at
    code level (no eval/exec path on caption content).
  - Minor-safe ack framing when `telegram_identities.is_minor = true`.
  - Phase-3 Routine trigger NOT wired (Phase 3 work).
- **Storage bucket `health-media` created** (private; no public access; signed URLs only).
  Created via `hs_ops.py setup-telegram-storage`.
- **`hs_ops.py` additions:**
  - `mint-link-code <profile_id>` — mints a single-use Telegram activation code (7-day expiry).
  - `setup-telegram-storage` — idempotently creates the `health-media` private Storage bucket.
  - `SUBJECT` list extended with all 5 new tables for ongoing `verify` coverage.
- **Tests — 9 Python integration tests** (`tests/unit/test_telegram_webhook.py`): tables exist,
  RLS enabled, media_inbox columns, update_id dedup (PK conflict on replay), media_inbox
  enqueue round-trip, injection caption stored-not-executed, unknown-chat no-row guard,
  push_log columns, link_code uniqueness. All 9 pass.
- **Deno unit tests** (`supabase/functions/telegram-webhook/webhook_test.ts`): secret-token
  correct/wrong/null/length-mismatch, guessKind food/workout/lab/dexa/unknown, injection
  text classified-not-executed, SQL injection and prompt injection are plain strings.
- **Auth model note:** Edge fn uses `service_role` (same as `whoop-webhook` — no user
  session in an inbound webhook). `healthspan_app` role (Phase-3 Routine) reads via
  `SET ROLE authenticated + JWT`; RLS policies cover both paths.

## v3.2.1 — schema-map completeness + generated-col doc fix (2026-06-05)
- **Migration 016c — schema-comment completion.** 016 commented 19 high-value tables;
  34 live tables (the v3 feature tables, the 5 `stg_*_review` staging queues, and the
  catalogs) had ZERO `pg_description` and were invisible to `docs/SCHEMA-MAP.md`. 016c
  adds table + column COMMENTs (MEANING + UNIT + TRAP) for every ACTIVE remaining table,
  marks the two proven-dead fossils (`users_extended`, `body_metrics_history`) `DORMANT`,
  and fills the one missed `profiles.is_maintainer` comment. **Coverage now 54/54 base
  tables** (52 documented + 2 dormant), `profiles` 12/12.
- **`supplement_intake_logs.taken_on` doc bug fixed at the source.** SCHEMA-MAP is
  GENERATED from comments, so the durable fix is the COMMENT, not the doc. 016c supersedes
  the 016 wording: `taken_on` is now documented as "GENERATED ALWAYS from `taken_at::date`
  — NEVER insert/update (Postgres 428C9); set `taken_at` only; valid as an ON CONFLICT
  target". The table comment now says ON CONFLICT key (not "upsert key"), so a hand-written
  PostgREST insert can't be misled into supplying it again.
- **`gen_schema_map.py` is now DB-driven** — it renders EVERY documented public base table
  (was a hardcoded list of 19, which would have hidden the 34 newly-commented tables),
  adds a `coverage: X of Y` header line, and a trailing `## Unmapped tables` section that
  lists any base table with no comments (empty = full coverage). A table can no longer be
  silently hidden by being absent from the generator's list.
- **Regression guard** — `tests/unit/test_contract.py::TestGeneratedColsLive` asserts
  `taken_on` is detected as GENERATED via live `pg_attribute` introspection (skips when no
  DB), so a locked-down-role permissions regression that silently degrades `_generated_cols`
  to empty fails loudly instead of reopening the 428C9 bug.
- _No PostgREST write guard added: there is no `.insert/.upsert` write path in the
  codebase (all writes go through the psycopg2 `contract.write`, which already strips
  generated cols) — a hardcoded strip-set would be dead code that drifts._

## v3.2 — App/claude.ai connection fixes (2026-06-05)

- **whoop-webhook bug fix (deployed):** `mapWorkout` never computed `duration_min` from
  `rec.start`/`rec.end` (the Python sync does, via `_duration_min`), so webhook-sourced workouts
  had NULL duration (e.g. the Jun-5 Fatmax Walk). Now computes it + the zone `*_pct` shares.
  `mapSleep` likewise gained `in_bed_duration_min` (from start/end) + `awake_min`,
  `sleep_need_min`, `sleep_debt_min`, `sleep_consistency_pct`. Existing NULLs backfilled.
- **P0 — `supabase_client` now authenticates.** `lib/db.get_app_connection` previously returned
  an UNauthenticated client (anon → RLS denied every read, 42501). It now calls
  `client.auth.sign_in_with_password({email, password})` with `auth_email` + `auth_password`
  from the config, verifies a session/JWT came back, and returns the authenticated client.
  Actionable errors: missing `auth_password` → "config.connection.supabase_client.auth_password
  required for App mode"; bad creds → the auth error surfaces, **never** an anon fall-through.
  (`lib/db.py` get_app_connection; config example gains `auth_password`; SKILL.md §0 step 2.)
- **psycopg2 import is now lazy** (`lib/db._psycopg2()`), so the pure App path imports `lib.db`
  and signs in WITHOUT psycopg2. `direct_role` without it → "pip install psycopg2-binary".
- **Pinned cold-start set** — `supabase==2.10.0`, `httpx<0.28`, `PyJWT==2.12.1`; `psycopg2-binary`
  moved to an optional `[direct_role]` extra (skipped by a plain `-r requirements.txt`). App
  bootstrap one-liner in SKILL.md: `pip install --break-system-packages --ignore-installed PyJWT
  "httpx<0.28" supabase` (clears the OS PyJWT 2.7.0 RECORD conflict). Pins verified via dry-run.
- **Schema-map freshness** — `gen_schema_map.py` stamps `generated_at`; `package_skill.py`
  regenerates the map into every bundle (best-effort); `self_test.py` WARNs if it's >30 days old;
  SKILL.md ad-hoc rule strengthened to "views catalog FIRST; ad-hoc only after loading
  SCHEMA-MAP.md" (a live tester guessed `whoop_sleeps.start_time`; real cols `sleep_onset`/`wake_onset`).
- **SKILL.md description trimmed to 923 chars** (was 1083 — over the 1024 install limit; the trim
  had only ever lived in packaged bundles, so repackaging kept regressing it).

## v3.1 — person-agnostic repackage (2026-06-04)
- **`scripts/self_test.py`** — first-class read-only self-test (never writes; no `query_audit`
  rows). Prints **STEP 0 "WHERE AM I RUNNING" first** — connection mode *with meaning*
  (direct_role = local Cowork/Code, does NOT prove App egress; supabase_client = App egress
  PROVEN), runtime signals (config source, `.git` up-tree, cwd, context source) → one-line
  `RUNTIME:`. Then 8 steps (version, identity, connection + RLS-scope proof, context targets,
  maintainer, freshness, capabilities, verdict). **VERDICT embeds runtime** —
  `READY (Cowork · direct_role)` vs `READY (App · supabase_client)`. Triggers in SKILL.md
  ("run a self-test", "verify the install", "what can you do").
- **`package_skill.py` excludes `context/`** (same as `config/`) — the bundle is now
  person-agnostic; per-person context is delivered separately (repo folder on Cowork/Code,
  Project knowledge on claude.ai). The stale v3 artifact over-shipped `pc/dea.context.md`.
- **Leak guard hardened**: aborts if any `context/*.context.md` is staged (a `.example.md`
  template is allowed), alongside the existing secret checks.
- **SKILL.md bootstrap amendment** (§0 step 1): with no filesystem config/context, read
  `<who>.config.json` + `<who>.context.md` from Project knowledge; if absent, ask the user to
  paste them once. Never proceed with assumed identity or targets.
- **Trigger description strengthened**: casual action phrases ("log this", "here's my meal",
  "save this", "how did I sleep", "how's my recovery", "am I on track", photo drops) + topics
  (creatine, protein, macros, calories, weight, fasting, injury/back, stress, mental health,
  onsen/contrast therapy, anti-aging, lung).
- Rebuilt `dist/healthspan-v3.1.skill` (87 files); leak guard clean; frontmatter parses.

## v3 — fossil cleanup round 2 + deploy + persistence proof (2026-06-04)
- **Migration 028**: dropped 4 more empty fossils — `user_telegram_links` (push is Google
  Chat via webhook, NOT Telegram — Telegram was never in the stack), `locations`,
  `log_type_config`, `source_priority_config`. KEPT `audit_log` (future write-mutation
  audit) + `canonical_aliases` (lab alias map, SGPT→ALT). Public tables 58 → **54**.
- **whoop-webhook DEPLOYED** (project `dsnydskkjwziynwmzfkh`, `--no-verify-jwt`) — the
  `sleep.*`→prior-cycle strain refresh is now live.
- **Persistence verified (committed evidence rows left in place):** a real catalog
  (`run_view`) + ad-hoc (`run_adhoc_audited`) run land committed `query_audit` rows; a real
  `source='photo'` supplement intake commits and persists.
- CLAUDE.md stack corrected: Messaging is Google Chat, not Telegram.

## v3-8 — WHOOP strain refresh + supplement source vocab (2026-06-04)
- **Stale-cycle strain fix.** WHOOP emits no `cycle.updated` webhook, so a cycle written at
  recovery-time keeps its ~0 day_strain until re-pulled. Two fixes:
  - `ingest/whoop_sync.py`: new reusable `refresh_recent(hours=48, profiles=…)` (mini sync
    over a recent window for all token-holding profiles, per-profile error isolation), plus
    `--since` relative windows (`48h`/`2d`) and `--all-profiles`. The morning brief (not yet
    built) will call `refresh_recent` before composing.
  - `supabase/functions/whoop-webhook`: on a `sleep.*` event (waking → the prior cycle just
    closed) it now re-fetches the latest CLOSED cycle and upserts its FINAL strain.
    Best-effort — a refresh hiccup never loses the sleep that saved. **Needs redeploy:**
    `supabase functions deploy whoop-webhook --no-verify-jwt`.
  - Verified live: PC's Jun-3 cycle day_strain 0.07 → **9.17**; Jun-3 running workout
    duration NULL → **14.2 min**.
- **Migration 027 — supplement source vocab.** `supplement_intake_logs.source` CHECK now
  allows `photo` (aligns with food_logs/biomarkers). `ingest/supplement.py` validates
  `source` against `_VALID_SOURCES` (mirrors food.py/biomarker.py `_VALID_METHODS`).
- **`lib/contract.write` now skips GENERATED columns** on INSERT/UPDATE while still allowing
  them as `ON CONFLICT` targets — fixes a latent bug where the skill's supplement-intake
  write always failed on the generated `taken_on` column (the 17 existing rows came via the
  journal path). Photo-sourced intake now writes cleanly (verified live).

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
- **Migration 026 (applied 2026-06-04, PC signed off)**: dropped 8 dead SaaS-fossil tables
  (`user_invites`/`user_invite_tokens`/`user_tier_history`/`user_locations`/
  `user_log_type_prefs`/`user_preference_history`/`brain_conversations`/`brain_messages`).
  Public tables 66 → 58.

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
