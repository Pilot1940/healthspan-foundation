# HealthSpan Skill — Changelog

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
