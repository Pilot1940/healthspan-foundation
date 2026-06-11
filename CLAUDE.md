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
- **Void a mis-logged row** (never DELETE) → §7.8 — `maintainer_void_supplement/_food/_biomarker(id, reason)`.

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

## Current State (2026-06-11)

- **strength_logs (mig 063) + per-item Viome flags + supplement dedup SHIPPED (v3.15.0, 2026-06-11).**
  (1) **strength_logs (mig 063):** new table for resistance training (`exercise`, `modality`,
  `load_value`/`load_unit`, `sets`, `reps`, `rir`, `device_specific`, `performed_on` GENERATED UTC
  date). Soft-delete via `voided_at`/`void_reason` + `maintainer_void_strength(p_id, p_reason)` —
  byte-for-byte mirror of mig 060's `maintainer_void_food`. RLS + grants copied from `food_logs`
  (FOR ALL on `has_profile_access`; authenticated S/I/U, NO DELETE, no `healthspan_app`). Verified
  live in the migration's own DO-block. Seed is SEPARATE (`scripts/seed_strength_063.py`,
  `--commit`) — transportable schema keeps personal data out of the migration. PC's 2026-06-11
  session seeded (trap-bar DL 165 lb 5×5; 2× cable lat-pulldown). **`machine_chest_press` HELD** —
  recorded `load_unit='plates'` value 140 (invalid unit; likely 140 lb stack); confirm before seeding.
  (2) **Viome flags now PER ITEM, not meal-wide (`inbox_drain.py`):** a multi-dish photo used to
  stamp the meal-wide verdict + the FULL flagged-ingredient list onto EVERY row, so one
  "mushrooms = superfood" showed as "Superfoods: mushrooms ×5" in the brief. Now each inserted row
  gets only the verdicts whose ingredient is its own (one viome lookup over the union, scoped back
  per item by `foods[]`/description terms). Confirmation summary stays meal-level. Regression test
  `test_food_list_viome_flag_is_per_item_not_meal_wide`; suite **283/0/9**. Today's 5 lunch rows
  corrected (only the fettuccine keeps mushrooms/superfood).
  (3) **Supplement catalog dedup (data):** `learn_supplement`'s normalized-name dedup let
  near-dups through. Merged 4 learned duplicates into canonical rows (2× B12, "Magnesium
  Biglycinate" typo → Bisglycinate, "Fish Oil" → Omega-3 — repointed intake logs, deleted dup
  rows); confirmed the 2 genuinely-new items (ashwagandha gummy, ketone electrolytes) so the
  brief's "Recently learned (review)" list is clean. Deeper fix (fuzzier dedup) is backlog.
  ⚠️ The inbox_drain.py flag fix goes live on push (CI runs `monitor/` directly, no deploy).
- **Today's food corrections (2026-06-11, with PC):** quiche halved (50% eaten), avocado bowl →250
  kcal, fettuccine meat removed (→135 kcal). Direct value edits on `food_logs` (not voided/re-logged).

## Current State (2026-06-10)

- **Backlog batch SHIPPED — #18 #20 #7 #21 (migs 060–062, 2026-06-10).**
  (1) **#18 void soft-delete (mig 060):** `voided_at`/`void_reason` on supplement/food/biomarker
  logs; `maintainer_void_supplement/_food/_biomarker(id, reason)` SECURITY DEFINER RPCs (§7.8);
  supplement+biomarker ingest RPCs **un-void on ON CONFLICT re-log** (else a re-logged item stays
  invisible); every read path filters `voided_at IS NULL` (brief food/supps/Viome, drain totals +
  orphan-sweep candidates, analysis/supplement_summary + food_chat, lib/views biomarker views,
  plan/goals, contract plausibility heuristic, daily_health_summary view; SKILL.md ad-hoc rule +
  SCHEMA-MAP regenerated); **REVOKE DELETE ON ALL TABLES FROM healthspan_app** — the 2026-06-08
  manual grant had landed on ALL 60+ tables (worse than recorded). Live-verified in a rolled-back
  txn: void → already_voided → un-void-on-relog → unauthorized-for-non-maintainer.
  (2) **#20 WHOOP token race FIXED:** on a refresh 400 the loser re-reads `whoop_tokens`, uses the
  winner's fresh access token or retries ONCE with the rotated-in refresh token — BOTH sides
  (Python `_get_token_from_db` + Deno `_shared/whoop.ts`); **whoop-webhook v17 + whoop-oauth v13
  deployed 2026-06-10**. Expect the ~1–2/day `WHOOP token 400` sync-error rows to stop.
  (3) **#7 media retention SHIPPED:** `monitor/media_retention.py` (stdlib, service_role) +
  `media-retention.yml` (22:00 UTC daily + dispatch w/ dry-run); window = `media.retention_days`
  (mig 062, 45). done/failed rows past the window: Storage object deleted, `storage_path` nulled,
  row kept. Live dry-run OK; first real prunes ~2026-07-22.
  (4) **#21 hygiene (mig 061):** `media_inbox`/`push_log` INSERT scoped to
  `has_profile_access OR is_maintainer` (drain inserts push_log as maintainer-drainer); open
  INSERT policies dropped on `telegram_processed_updates`/`wearable_sync_errors`; legacy mig-002
  fns (`ingest_health_artifact`/`mint_ingestion_token`) EXECUTE revoked from anon+authenticated;
  dead keys deleted (`ingest.whoop_screenshot.direct_write`, `supplements.journal_intake_map`,
  `supplements.intake_source_priority`); unused `claim_inbox_cluster` DROPPED — the drain claims
  per-item (`claim_inbox_item` CAS + release-on-partial-loss; SYSTEM.md §3 corrected);
  `hs_ops verify` rewritten: runtime-derived profile-scoped table list (42/61), leak check incl.
  `with_check=true`, maintainer-only assertion covering SELECT **and ALL** policies. Live verify
  all NONE OK. Unit suite **282 passed / 0 failed / 9 skips**. BACKLOG: #13 rewritten as the
  Telegram on-demand query/command design (PC to scope v1 — includes "today's meals + macros",
  verbose review, /learn, undo-last); #22 opened (telegram-webhook credential-less test gap).
- **whoop-webhook v3: recovery webhooks FIXED (BACKLOG #19, 2026-06-10).** v2 `recovery.updated`
  payload id is the SLEEP UUID (docs-confirmed), not a cycle id — the handler 404'd on every
  recovery event since launch (110 fails/week; `recovery_landed` pushes never fired). Now resolves
  via `/v2/recovery` collection keyed `sleep_id` → integer `cycle_id`; also fixed the reversed
  `/v2/recovery/cycle/{id}` → `/v2/cycle/{id}/recovery` in BOTH call sites (old shape always 404'd
  silently — webhook cycle rows never carried recovery). Deployed + verified end-to-end with a
  signed synthetic event: first `webhook:recovery.updated` success + first `recovery_landed` push
  ever (07:04 UTC). Note: PC's whoop_tokens row was manually refreshed during verification (rotated
  pair stored back). Expect `webhook:recovery.updated` successes from tomorrow morning's real event.
- **Clarify auto-match shipped (BACKLOG #15 closed, mig 059, 2026-06-10).** PC's call: Dea (14)
  will never use Telegram replies, so the system absorbs the fresh-message habit. Layer 1
  (`absorb_pending_clarify` in `monitor/inbox_drain.py`): a fresh TEXT message within
  `clarify.match_window_sec` (900s) of a pending clarify for the same chat is LLM-judged; at
  ≥`clarify.match_min_conf` (0.7) it's processed AS the clarification (orig caption + image) and
  the stranded item retires — identical semantics to the webhook reply path. On any doubt →
  independent processing (orphan is recoverable; wrong merge corrupts). Layer 2
  (`sweep_staged_orphans`, end-of-run): staged item whose profile logged a matching prod row
  within `clarify.orphan_supersede_window_sec` (1800s) retires as superseded (result_ref → prod
  row) — covers the photo-follow-up shape all 5 historical orphans had. Mig 059 also added
  maintainer UPDATE policies on the 5 stg_*_review tables (drain passes `is_maintainer()` via its
  membership to PC's maintainer profile — pre-existing mechanism). 13 unit tests; suite 272/0/9.
  Reply path unchanged. ⚠️ Note: the drain identity satisfying `is_maintainer()` is a pre-existing
  quirk now load-bearing in two places (maintainer_ingest_* guards + stg UPDATE policies).
- **Full deep scan 2026-06-10 (16-agent workflow: schema vs migrations vs docs vs code, RLS
  audit, test health + live data checks).** Schema↔migrations CLEAN (045–057 all verified
  present, no orphan tables, 61 base/7 views); unit suite GREEN (259/0/9 skips); all RPC
  call-sites match live signatures; model ids current. Three real issues found:
  (1) **mig 058 APPLIED 2026-06-10** — mig 022 missed maintainer-only RLS on
  `stg_supplement_intake_review`/`stg_food_rule_review`/`stg_test_result_review`; 058
  rebuilt them (maintainer SELECT + owner INSERT, verified live), seeded
  `brief.dedup_sec=600` (was code-fallback only, rule-#1 violation) and versioned the
  out-of-band `rls_auto_enable` event-trigger function.
  (2) **whoop-webhook `recovery.updated` ALWAYS 404s** (110 failed runs/week; the v2
  recovery webhook id is a UUID but the handler GETs `/v2/cycle/{id}` which takes an
  integer) — `recovery_landed` pushes have NEVER fired; data still lands via
  `refresh_recent`. BACKLOG #19. Also ~1–2/day WHOOP token-400s = refresh-rotation race
  between webhook and CI `refresh_recent` (self-heals; BACKLOG #20).
  (3) Docs were materially stale — fixed: SYSTEM.md §2.2/§2.3/§3.2 (removed-path
  text→send-brief dispatch), §3.4/§5.1 Haiku→Sonnet, §4.1 recount 45/16 of 61 (+13
  missing active tables incl. `push_log`), §4.2 (+6 undocumented DEFINER fns), §6
  onboarding drainer-UID (was PC's UID — would have broken Nanki onboarding), CLAUDE.md
  stale counts, BACKLOG #5/#6/#14/#16 closed. **Review queue cleared 2026-06-10 (with PC)**:
  all 6 pending stg rows + 2 staged media_inbox were ORPHANS of successful follow-up logs —
  PC re-sent each item as a fresh message instead of replying (Hooray shake → logged 12:17
  as "Lactose Free Protein Shake 340ml"; Thai tea 1/4 → logged 12:21 as "PROTEN 1/5 bottle";
  "took a supplemen" → Magnesium Bisglycinate logged 55s later; electrolytes clarify chain →
  MSKE 1 package logged 00:28). Retired: 4 merged (content in prod, media_inbox result_ref →
  superseding food_logs ids), 2 rejected (superseded clarify intermediates). Same
  fresh-message-instead-of-reply pattern as BACKLOG #15 — now 5 occurrences, design question
  getting warmer.
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
  `test_run_once_textonly_brief_still_briefs`. (The ~15 `test_inbox_drain`/`test_brief` failures were
  long mislabeled "environmental — real-API 401"; the 2026-06-09 deep scan disproved that — they were
  deterministic STALE-CONTRACT tests. **FIXED in commit `930b734`** (test-files-only); suite verified
  green 2026-06-10: 259 passed / 0 failed / 9 env-gated skips. BACKLOG #16 closed.)
- **Migrations 050–053 applied** — 050 food kcal floor 25→0; 051 supplements learned stamps;
  052 `learn_supplement` RPC; **053 storage drainer-read RLS** (above).

- **Migrations 024–028 all applied** (024 drop-backup, 025 query_audit reshape, 026 drop 8
  dead SaaS tables, 027 supplement source vocab +photo, 028 drop 4 more fossils —
  user_telegram_links/locations/log_type_config/source_priority_config; KEPT audit_log +
  canonical_aliases). **54 public tables** at that point (61 now, after migs 029–057). No
  `schema_migrations` table: applied DB state is the source of truth; apply via
  `python scripts/hs_ops.py apply <file>`.
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
- **61 base tables** (45 active / 16 empty, 2026-06-10 snapshot), 7 views. Historical
  inventory: `docs/HEALTH-CHECK-2026-06-03.md` (pre-mig-036 — counts superseded; current
  inventory in SYSTEM.md §4.1 + SCHEMA-MAP).
- **Maintainer model** (022/023): PC is the sole maintainer (`profiles.is_maintainer`,
  resolved via `family_memberships`). Maintainer-only RLS SELECT on `query_audit`,
  `wearable_sync_log`/`_errors`, `stg_*_review`. Dea sees outcomes, never the machinery.
  (The 2026-06-10 deep scan found mig 022 had MISSED three staging tables —
  `stg_supplement_intake_review`, `stg_food_rule_review`, `stg_test_result_review` still
  had the mig-004 ALL/has_profile_access policy. **Fixed by mig 058, applied 2026-06-10**,
  verified live.)
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
