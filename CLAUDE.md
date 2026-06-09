# Healthspan Foundation — Claude Rules

## 📖 Definitive Guide & Maintenance — read these first

**`docs/SYSTEM.md`** is the single definitive manifest of this system — overview, infrastructure,
event-driven pipelines, full schema, codebase map, new-user onboarding, and the **maintenance
runbook (§7)**. Read it (Markdown is the LLM-friendly source) before doing any maintenance.
**`docs/SYSTEM.html`** is the same content as a styled, navigable page for humans
(`file://…/docs/SYSTEM.html`). Keep the two in sync — when you change `SYSTEM.md`, regenerate the HTML.

**`BACKLOG.md`** is the living record of what's open, what's shipped, and how things were decided.
Check it for context before building; log new gaps and shipped fixes there.

Common maintenance tasks → SYSTEM.md §7:
- **Clear the review queue** ("N to review") → §7.2a — impersonate maintainer, `maintainer_ingest_*`
  with `p_force_stage := false`, then retire the `stg_*_review` + `media_inbox` rows.
- **Force a stuck drain** → §7.2 (`gh workflow run inbox-drain -f settle_sec=0`).
- **Apply a migration** → §7.1 (`python3 scripts/hs_ops.py apply migrations/NNN_*.sql`).
- **Deploy an edge function** → §7.4 (run from repo root; watch for the "No change found" CLI skip).
- **WHOOP sync / score states** → §7.3. **Rotate secrets** → §7.6. **Regenerate SCHEMA-MAP** → §7.5.

This file (CLAUDE.md) + the auto-memory under `memory/` carry the *latest* deltas; SYSTEM.md carries
the *full* picture. If they disagree, the live DB and this file win — then update SYSTEM.md.

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

## Current State (2026-06-09)

- **mig 057 + end-to-end deep scan (2026-06-09).** `reticulocyte_count` metric_definition added
  (mig 057, commit `05669dc`) — last marker of the Jun 2026 Metropolis panel; reading 2.0% (normal).
  A scoped 5-dimension scan (biomarker integrity, brief pipeline, schema/RLS, docs drift, test health)
  ran via Workflow + advisor. Verdicts: brief pipeline + schema/RLS clean; docs were materially stale
  (now fixed — SYSTEM.md/html migration history → 057, table count 61 base/7 views, Haiku→Sonnet
  contradiction, minor-brief narrative). **Two data-hygiene flags on the reticulocyte row** (manual
  backfill, bypassed staging): `measured_at` Jun 6 vs the panel's Jun 5 (likely off-by-one; unverifiable
  without the source PDF — NOT auto-changed), and `source='lab_report'` vs the panel's `blood_test`
  (cosmetic; `biomarkers.source` is free-text, nothing consumes it). Both left for PC to confirm.
- **Explicit brief request bypasses the post-log dedup (commit `d2d5d3d`, 2026-06-09).** The cross-run
  `brief.dedup_sec` (600s) is for collapsing burst post-log briefs; it was wrongly swallowing an
  explicit "how am I doing?" sent shortly after a food-log. Now only auto post-log briefs dedup; an
  explicit request always sends. Also guarded: `push_log` only records `'sent'` when `compose_brief`
  actually sent (truthy return), not for an inactive-identity no-op.
- **Minors can now receive the daily brief via per-profile consent (mig 056, 2026-06-09).**
  Previously a minor got log confirmations only — never the full brief (food totals + WHOOP +
  plan), because both brief triggers excluded `is_minor`. New `system_config` key
  `brief.minor_optin_profile_ids` (JSON array of profile_ids) is an explicit maintainer-consent
  allowlist: a minor gets a brief (post-log trigger in `monitor/inbox_drain.py:run_once` AND the
  `send-brief --all` scheduled trigger in `monitor/brief.py`) ONLY if listed. **Dea is opted in**
  (PC consented as father+maintainer); the dormant Dev slot / any future minor stays brief-off by
  default. Brief content was already minor-safe (no deficit language, no Viome, no "learned" list).
  Side-effect: WHOOP now surfaces for Dea — `refresh_recent` runs inside `compose_brief`, so a minor
  who never got a brief never had her WHOOP refreshed. Verified: Dea's token refreshes + syncs live
  (3 workouts/3 cycles/2 sleeps on 2026-06-09). Tests: `test_run_once_minor_{no_,}brief_with{out,}_consent`.
  Goes live on push — CI runs `monitor/` directly (no edge-function deploy).
  Phantom review row from the same incident **cleaned 2026-06-09**: `stg_food_log_review` 906847f9
  ("fried chicken") → `merged`, orphaned `media_inbox` 3f831e27 → `done`. Real cause was NOT a
  carry-through bug — Dea's first correction ("banana bread") arrived as a *fresh message, not a
  Telegram reply* to the clarify prompt, so the staged photo never entered the clarify-match path and
  was independently logged; the item is correctly logged as `d6c9fb01`. No safe narrow code fix (linking
  a free-text message to a pending staged item without a reply pointer would mis-attach). Open design
  question, not a bug — see BACKLOG #15.

- **Model ids centralized + token-efficiency fixes (commit `bc672c9`, 2026-06-09).** `lib/models.py`
  is the single source of truth (`HAIKU`/`SONNET`) + `models.create_message()` which turns a
  retired/invalid model id into a clear error instead of a silent 404. Fixed `ingest/food.py` (was
  hardcoded `claude-3-5-haiku-20241022`, **retired 2026-02-19** → would 404). `inbox-drain.yml` has a
  `concurrency` group (serialize drains). Cross-run **brief dedup** via `push_log` (`brief.dedup_sec`,
  default 600s) — no more multiple briefs from a burst of logs. **Skipped** image/prompt caching (#3/#5):
  net-negative on the common single-vision path. All live via CI (no deploy).
- **ONE model everywhere = Sonnet 4.6 (commit `d4e2590`, 2026-06-09).** Per request, dropped the
  Haiku/Sonnet split: brief + clustering + food-classify now Sonnet (was Haiku); removed the
  `drain.cluster_model` knob (clustering reuses the vision model). `lib/models.py` keeps `HAIKU`
  available for a per-path `system_config` override (e.g. `brief.model`) but nothing uses it by
  default. Cost delta ~+$0.50/mo (vision dominates). App cost estimate ≈ **$6–12/mo** for 2 active
  adults (~1.3¢/photo) — see SYSTEM.md §2.6.
- **Staged item clarified → its review row is retired (mig 055, commit `ce91936`, 2026-06-09).**
  A staged food item the user clarified used to leave its `stg_food_log_review` row `pending`
  forever (phantom in the maintainer review queue; no back-link to `media_inbox`). Drain now records
  `staged_review_ids` on the row; the webhook marks those review rows `merged` on clarify-supersede.
  Mirrors `logged_food_ids` (mig 054). ✅ **telegram-webhook v7 deployed 2026-06-09** — supersede +
  orphan-retire are LIVE (script size 68.31kB; the CLI "No change found" skip needs a deploy-marker
  bump per SYSTEM.md §7.4).
- **Live-test results (2026-06-09, drain@176c53b):** ambiguous shake photo → **staged + asked** (C3
  PASS, conf 0.18, didn't auto-log a 300-kcal guess); reply re-extracted to **ONE** entry, 25g protein
  (honored user-stated macros, no double-count); image now genuinely read end-to-end (mig 053). C1
  re-verified, C3 PASS, C4 (logged-supersede) pending webhook deploy. Maintainer review queue clean.
- **Reply-to-correct supersedes a logged food item (mig 054, commit `7c635fe`, 2026-06-09).**
  Replying to an AUTO-LOGGED item used to double-count (the reply-to-clarify loop only matched
  STAGED rows). Now the drain stores `clarify_message_id` + `logged_food_ids` on inserted **food**
  rows; the telegram-webhook reply handler matches staged OR logged items and, for a logged item,
  **DELETEs those `food_logs`** (service_role) before re-queuing the correction → re-extraction
  REPLACES, not adds. Hard-error path now messages the user (no silent vanish after a supersede).
  **⚠️ supplements still double-count on correction** (deferred — needs a kind-agnostic link, not
  `uuid[]`; backlog). ✅ **telegram-webhook v7 deployed 2026-06-09** — the supersede path is LIVE.
- **Ambiguous food photos now STAGE instead of auto-logging a guess (same commit).** Vision prompt
  sets confidence ≤0.2 when naming a food by guessing ("appears to be X or Y") or when it can't
  identify the specific item from the image — so it asks rather than logging a guessed kcal. Also
  HONORs user-stated macros/portions from a caption/clarification verbatim (the 34g-vs-25g miss).
- **Drain could NEVER read image pixels — fixed (mig 053, 2026-06-09).** Root cause: `storage.objects`
  has RLS **enabled with ZERO policies** and `health-media` is private. The telegram-webhook uploads
  with the **service-role key** (bypasses RLS) so objects land fine, but the drain reads as the
  **authenticated** service-account user `healthspan.drainer@chitalkar.com`, which RLS **denied** →
  `get_signed_url()` returned NULL → `image_blocks` empty → the vision model never saw the image.
  Symptom: caption-less photos (e.g. a French press) classified as `brief`; captioned photos were
  extracted from the **caption TEXT only**, never the pixels. **mig 053** adds a single `SELECT` policy
  granting ONLY the drainer read access to `health-media` (UID resolved by email, mirrors mig 035;
  works across ALL profiles' media — the drain is the shared ingestion identity). End-users still have
  no direct media access; service_role still bypasses for uploads. Verified at the RLS layer (drainer
  SELECT allowed, other authed user denied). ⚠️ Storage RLS for end-user media access (if ever needed)
  is still unbuilt — only the drainer is granted.
- **Photo never routes to `brief` (commit `aad3f4d`, 2026-06-09).** Defense-in-depth in
  `monitor/inbox_drain.py:_process_cluster`: when an image is attached but the vision model returns
  `{"kind":"brief"}`, the drain now STAGES for clarification (the C3 path) instead of silently
  composing a brief — `if kind == "brief" and image_blocks: …`. With mig 053 the image now reaches
  the model, so this is the residual-ambiguity backstop. Tests: `test_run_once_photo_never_briefs`,
  `test_run_once_textonly_brief_still_briefs`. (⚠️ The ~15 `test_inbox_drain`/`test_brief` failures were
  long mislabeled "environmental — real-API 401"; the 2026-06-09 deep scan **disproved that** — they are
  deterministic STALE-CONTRACT tests against intentionally-evolved code, NOT API/network failures. Fix
  green, don't xfail — see BACKLOG #16.)
- **Migrations 050–053 applied** — 050 food kcal floor 25→0; 051 supplements learned stamps;
  052 `learn_supplement` RPC; **053 storage drainer-read RLS** (above).

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
  **047 (applied 2026-06-08)** reverts `fn_media_inbox_notify` to NO auth header (vault
  unreachable from pg_net → 401 stalls); trigger-drain runs no-auth (safe — only fires a GH
  dispatch). **048 (applied 2026-06-08)** seeds `app.timezone='Asia/Bangkok'` in `system_config`.
  **049 (applied 2026-06-08)** `media_inbox.clarify_message_id` + `clarify_count` for the
  reply-to-clarify loop.
- **Reply-to-clarify loop (2026-06-08, telegram-webhook v6)**: a staged item gets an LLM-written
  "what's unclear — reply to fix it" message (`describe_stage`, minor-aware); the drain stores that
  message id on the staged rows (`telegram_send` returns it). A user REPLY → webhook INSERTs a fresh
  `media_inbox` row (orig caption + `[clarification: …]` + orig image) so the **AFTER-INSERT** trigger
  fires (UPDATE wouldn't); orig row retired; LLM re-extracts. `clarify_count` caps at 2 → hands to PC.
  Supplements now log without a dose (`supplement_is_complete` = id-only; dose defaults from regimen).
- **Brief energy balance (2026-06-08)**: expenditure = BMR (`calorie_floor` from context) + WHOOP
  activity, not just the activity burn — fixes the false surplus. Adult-only line (minors: growth framing).
- **LLM-routed text (2026-06-08, telegram-webhook v15 + inbox_drain)**: the regex gate is GONE.
  ALL text-only Telegram messages enqueue to `media_inbox` (kind=unknown); the drain's `unknown`
  prompt classifies each as a LOG (food/supplement/biomarker, **multi-item**) or `{kind:"brief"}`.
  Brief requests compose inline at end-of-run (`run_once` → `compose_brief`). Supplements match on
  `display_name` and **default their dose from the user's active regimen** (`lookup_regimen_dose`),
  so "i took D3, K2, B12, magnesium citrate, omega-3" auto-logs all five. Vision now reads packaged
  nutrition labels (incl. Thai). `guessKind` survives only as a photo-caption hint.
- **Daily brief = LOCAL day (2026-06-08, brief.py)**: "today" is the `app.timezone` day computed as a
  UTC window over `logged_at`/`taken_at` (not the UTC-derived `log_date`/`taken_on`). Fixes the
  07:00-ICT rollover + UTC display. Every brief also runs `refresh_recent` first (WHOOP refresh-on-
  interaction); WHOOP staleness is elapsed-time (>30h), tz-safe. ⚠️ Write-side `log_date`/`taken_on`
  are still UTC-derived — non-brief consumers of those columns aren't yet tz-localized (backlog).
- **Drain service account**: `healthspan.drainer@chitalkar.com` (HS_AUTH_EMAIL env var).
  UID resolved dynamically in migration 035 — no hardcoded UUID.
- **Event-driven drain pipeline**: Telegram photo → `media_inbox` INSERT → pg_net trigger
  `fn_media_inbox_notify` → `trigger-drain` Edge function (15s dedup via system_config CAS)
  → GitHub `repository_dispatch` `inbox-drain` → GH Actions → `monitor/inbox_drain.py --settle-sec 5`.
  **Text messages** also enqueue to `media_inbox` (no regex routing); the drain's LLM decides
  log-vs-brief and either writes the data or composes the brief inline. `send-brief.yml` remains for
  cron/manual briefs (`workflow_dispatch -f profile_id=…`).
- **WHOOP sync**: `ingest/whoop_sync.py` — `sync_profile()` runs workouts + cycles + sleeps +
  body_measurements for each profile. `--backfill` from 2020-01-01. `refresh_recent()` for
  brief pre-sync. Score state tracked: SCORED / PENDING_SCORE / UNSCORABLE — brief shows ⏳
  for pending, ❌ for unscorable. WHOOP body endpoint: height_m, weight_kg, max_heart_rate.
  whoop-webhook redeployed 2026-06-08 (v13, `*.deleted` handling). WHOOP_CLIENT_ID/SECRET are GH
  secrets + in inbox-drain.yml + send-brief.yml so `refresh_recent` has creds in CI. No scheduled
  WHOOP cron yet — webhook + refresh-on-interaction cover it (backlog #8).
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
  **Nanki** (PC's wife, 45F adult) is a supported profile to onboard when ready — full adult
  framing (like PC), non-maintainer; onboard exactly like Dea but `relationship` ≠ `child` so
  `is_minor=false`. Dev = dormant slot.
- **Learn-on-clarify (049/051/052)**: a SPECIFIC supplement the user names that isn't in the
  catalog auto-adds via `learn_supplement` RPC (dedups by normalized name; stamps
  `source='learned'`, `verified=false`) — ADULTS only (minor's new item routes to PC). PC's
  brief shows "🆕 Recently learned (review)" to prune. Brief ends with `—v <date>·<commit7>`
  (GITHUB_SHA) for debug. `/learn` was a phantom command (removed); food auto-promote is backlog #13.
- **Query logging** (025): every skill read lands in `query_audit` —
  `lib/views.run_view` (catalog) + `lib/sql_guard.run_adhoc_audited` (adhoc) via
  non-blocking `log_query`. Maintainer digest: `monitor/query_log.py`.
- v3 modules: `lib/` (context, views, sql_guard, db, contract), `ingest/`, `plan/`,
  `monitor/`, `analysis/`, `export/`. SCHEMA-MAP regenerated via `scripts/gen_schema_map.py`.
