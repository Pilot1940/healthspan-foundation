# HealthSpan Skill — Changelog

## v3.11.0 — Write-contract audit: fix all RPC→table constraint gaps (2026-06-07)

**Proactive audit of all three RPC INSERT paths against live table constraints.**

| Gap | Table | Constraint | Drain value | Fix |
|-----|-------|-----------|-------------|-----|
| supplement source CHECK missing 'telegram' | supplement_intake_logs | `source_check` | `'telegram'` | 040: add 'telegram' to CHECK |
| supplement_id NOT NULL, no RPC guard | supplement_intake_logs | NOT NULL | `None` if no lookup | 040: DB guard → stage with reason |
| supplement RETURN used `p_stage_reason` not `v_stage_reason` | — | — | — | 040: use `v_stage_reason` |
| metric_definition_id NOT NULL, no RPC guard | biomarkers | NOT NULL | `None` if unresolved | 040: DB guard → stage with reason |
| value NOT NULL, no RPC guard | biomarkers | NOT NULL | `None` if unresolved | 040: DB guard → stage with reason |
| meal_type CHECK excludes 'unknown' | food_logs | `meal_type_check` | AI could return `'unknown'` | 040: DB guard → stage; Python: remove 'unknown' from food prompt |
| description NOT NULL, no RPC guard | food_logs | NOT NULL | `None` if missing | 040: DB guard → stage |

- **Migration 040** (`040_write_contract_audit.sql`):
  1. `supplement_intake_logs.source_check`: DROP + re-ADD with `'telegram'` in array
  2. `maintainer_ingest_supplement` (040): local `v_stage/v_stage_reason`; supplement_id NULL guard; fixed RETURN to emit `v_stage_reason`
  3. `maintainer_ingest_biomarker` (040): metric_definition_id + value NULL guards (before plausibility gate)
  4. `maintainer_ingest_food` (040): description NULL guard + meal_type guard (`NOT IN valid set AND NOT NULL → stage`)
  - Verify block asserts all 4 changes are present in live DB
- **Python** (`monitor/inbox_drain.py`): `_VISION_PROMPTS["food"]` meal_type changed from `breakfast|lunch|dinner|snack|supplement|unknown` → `breakfast|lunch|dinner|snack|drink|supplement` (matches the CHECK exactly; 'unknown' was causing CHECK violations)
- **Integration tests** (5 new in `tests/integration/test_supplement_biomarker_rpc.py`):
  - `test_040_telegram_source_inserts_real_row` for all 3 RPCs — would have caught source CHECK gaps on any table
  - `test_040_null_supplement_id_db_guard_stages` — supplement NULL ID guard
  - `test_040_null_metric_id_db_guard_stages` — biomarker NULL metric guard
  - `test_040_invalid_meal_type_db_guard_stages` — food 'unknown' meal_type guard
  - `test_040_null_description_db_guard_stages` — food NULL description guard
- **Unit tests** (5 new, 83 total): food prompt meal_type options all-valid; source='telegram' in all 3 `_rpc_args` helpers

## v3.10.1 — Fix: supplement RPC 428C9 (GENERATED taken_on) (2026-06-07)

- **Root cause:** `supplement_intake_logs.taken_on` is `GENERATED ALWAYS AS ((taken_at AT TIME ZONE 'UTC')::date)`.
  Migration 038 included `taken_on` in the INSERT column list and VALUES — PostgreSQL error 428C9
  ("cannot insert into a generated column") on every non-staged supplement write.
- **Migration 039** (`039_supplement_rpc_fix_generated_taken_on.sql`): drops and recreates
  `maintainer_ingest_supplement` with `taken_on` removed from INSERT column list and VALUES.
  `taken_on` is retained as the ON CONFLICT target (valid for GENERATED columns). `taken_at`
  continues to drive the expression; `taken_on` is auto-populated by Postgres.
- **No biomarker equivalent** — `biomarkers` table has no GENERATED columns; no change needed.
- **Verify block** in migration asserts: `"taken_at, source"` adjacent (no `taken_on` between them)
  and `taken_on` present in `ON CONFLICT` target.
- **Live verified** via psql: `force_stage=FALSE` call → `{status: "inserted", taken_on: "2019-12-01"}`;
  no 428C9.
- **Integration tests** added in `tests/integration/test_supplement_biomarker_rpc.py` (skip when
  `HS_AUTH_EMAIL`/`HS_AUTH_PASSWORD` absent): INSERT path asserts `taken_on` auto-populated;
  staging path asserts `stage_reason` written.
  - Note: integration test uses `source='manual'` — `supplement_intake_logs_source_check` allows
    only `['manual','journal','skill','csv','photo']`; drain's `'telegram'` source would fail
    this constraint on the INSERT path (separate issue, not part of 039).

## v3.10.0 — Drain: supplement + biomarker alignment (2026-06-07)

- **Root cause fixed:** live 400 on `maintainer_ingest_supplement`. Two failure modes in the
  old confidence-routed RPC: (a) matched supplement + `taken_at=NULL` → NOT NULL violation on
  `supplement_intake_logs.taken_at`; (b) unmatched supplement + `extracted_name=NULL` →
  NOT NULL violation on `stg_supplement_intake_review`. Both resolved.
- **Migration 038** (`038_supplement_biomarker_align.sql`): drops both old 10-param RPCs; creates
  12-param versions that mirror `maintainer_ingest_food` (036/037):
  - `p_force_stage boolean DEFAULT FALSE` + `p_stage_reason text DEFAULT NULL`
  - Route on `p_force_stage` (Python completeness gate), not confidence threshold
  - `COALESCE(p_taken_at, now())` + explicit `taken_on` set in supplement prod INSERT
  - `COALESCE(p_extracted_name, 'unknown')` in both staging INSERTs
  - `maintainer_ingest_biomarker`: DB-side plausibility gate using `metric_definitions.plausible_min/max`
  - Same guard (`is_maintainer() AND has_profile_access`), same grants (revoke PUBLIC/anon, grant authenticated)
  - Returns `{id, status, stage_reason}` jsonb; `NOTIFY pgrst` at end
  - `stage_reason text` added to `stg_biomarker_review` and `stg_supplement_intake_review`
- **Per-kind completeness gate** in `monitor/inbox_drain.py`:
  - `supplement_is_complete(extracted)` — requires resolved `supplement_id` + `dose_amount` + `dose_unit`
  - `biomarker_is_complete(extracted)` — requires `metric_definition_id` + `value` + `unit`
  - Supplement branch uses `force_stage=True` + kind-specific reason when incomplete
    (`"incomplete: no supplement match"` or `"incomplete: missing dose or unit"`)
  - Biomarker branch same pattern (`"incomplete: no metric match"` / `"incomplete: missing value or unit"`)
  - `write_supplement()` and `write_biomarker()` now accept `force_stage` and `stage_reason` params
  - `_supplement_rpc_args()` and `_biomarker_rpc_args()` helpers added (mirror `_food_rpc_args`)
- **Improved `unknown` prompt** — explicit per-kind schemas replace the vague `{...kind-specific fields...}`:
  - Supplement: single-object `data` with dose fields
  - Food: list `data` (even for single item) → after re-dispatch, food branch handles `isinstance(extracted, list)` correctly for multi-item text
  - Lab/DEXA: `data.biomarkers` list
  - Unclassifiable: `{"kind":"unknown","data":{}}`
- **`else` branch reason**: `kind == "unknown"` after re-dispatch → `"could not classify"`;
  `kind == "workout"` etc. → `"unknown kind: {kind}"` (preserves existing test)
- **Tests** (+17, 78 total): `supplement_is_complete` (4), `biomarker_is_complete` (4),
  supplement completeness gate (3: auto-write / missing-dose / no-match), `write_supplement`/
  `write_biomarker` forward `force_stage` (3), unknown-kind reclassification to food-list /
  supplement / stays-unknown (3).

## v3.9.0 — Drain: persist stage_reason on media_inbox + stg_food_log_review (2026-06-07)

- **`media_inbox.stage_reason text`** column added (migration 037). Every `mark_rows` call for a
  staged or failed item now writes a human-readable explanation — e.g.
  `"incomplete: missing calories or macros"`, `"implausible calories: 15000 kcal (must be 25–12000)"`,
  `"vision returned no parseable extraction"`, `"unknown kind: workout"`, `"connection timeout"`.
- **`stg_food_log_review.stage_reason text`** column added. The `maintainer_ingest_food` RPC
  (now 16 params, adds `p_stage_reason text DEFAULT NULL`) writes the reason into the staging
  row so the future daily Telegram review-summary can show WHY each item needs review.
- **DB-side kcal gate** now generates its own reason string
  (`"implausible calories: N kcal (must be 25–12000)"`) and returns it in the jsonb result.
  Python reads it and propagates to `media_inbox.stage_reason`.
- **Reason provenance** — clear ownership:
  - Python-determined (completeness gate) → `p_stage_reason="incomplete: missing calories or macros"` sent to RPC
  - DB-determined (kcal plausibility) → `p_stage_reason=NULL`; DB computes and returns in jsonb
  - Vision parse failure → `"vision returned no parseable extraction"` (existing sentinel, now persisted)
  - Hard error → error message string written to `stage_reason` on the `failed` row
  - Unknown kind → `"unknown kind: {kind}"` sent to RPC
- **`mark_rows()`** gains `stage_reason: str | None = None`; only adds the key to the PATCH
  body when non-None — no change to existing callers that don't pass it.
- **`_write()`** extracts `stage_reason` from RPC jsonb result and passes to `mark_rows`.
- **`_food_rpc_args()` / `write_food()`** gain `stage_reason=None` param.
- **Migration 037** (`037_stage_reason.sql`): adds both columns + rebuilds 16-param fn + verify block.
- **Tests** (+8, 61 total in drain file): `mark_rows` includes/omits stage_reason; vision parse
  failure, vision error, incomplete food, DB kcal gate, unknown kind, successful write (absent).

## v3.8.0 — Drain richer confirmations: running totals + end-of-run summary (2026-06-07)

- **Running food totals appended to each successful food write.** After `maintainer_ingest_food`
  returns `inserted`, the drain fetches today's `food_logs` totals via PostgREST and appends a
  one-line summary to the Telegram confirmation:
  - Adult: `Today: 950 / 2100 kcal (45%) · 65 / 180g protein · Supps 4/16`
  - Minor (Dea): `Today: 1200 kcal · 45g protein` — no target percentages, no adult framing;
    low-intake flag uses growth/performance language only ("keep fuelling to stay strong 💪").
- **Supplement status included** for non-minor profiles: counts taken vs active regimens today
  (from `supplement_intake_logs` vs `supplement_regimens`). Two PostgREST calls; best-effort
  — returns `{}` silently on any error so the confirmation still sends.
- **Targets from context MD** (`context/<slug>.context.md`). `_load_profile_contexts` reads the
  file for each active Telegram identity at run start (slug = `display_name.lower()`; e.g. "PC"
  → `context/pc.context.md`). Graceful on missing file. `daily_calories` and `protein_g` from
  the `## Targets / norms` section are used as comparison denominators.
- **`telegram_identities` select extended** to `chat_id,is_minor,profile_id,display_name` so
  context MDs can be loaded without an extra DB query.
- **End-of-run summary** — after all clusters processed, one message per chat with any
  activity: `Done — 2 logged, 1 to review.` No message for failure-only runs.
- **`_process_cluster` signature**: added keyword-only `profile_ctx`, `per_chat`, `today` —
  all optional with defaults, so every existing caller and test is unaffected.
- **New functions** (all PostgREST, no psycopg2):
  - `_load_profile_contexts(identities) -> dict[str, dict]`
  - `fetch_today_food_totals(db, profile_id, today) -> dict`
  - `fetch_today_supplement_counts(db, profile_id, today) -> dict`
  - `_totals_line(totals, supp, target_cal, target_protein_g, is_minor) -> str`
- **Tests** (+17, 53 in drain file, 196 total / 9 skipped): `fetch_today_food_totals`
  (sum, empty, null-calories, DB error); `fetch_today_supplement_counts` (taken/total,
  no regimens, none taken); `_totals_line` (adult with/without targets, minor no-deficit,
  minor low-intake positive framing, empty returns ''); end-of-run summary (written,
  staged, failure-only-no-message); totals appended on write / not on stage.

## v3.7.0 — Drain extraction fixes + completeness-based food gating (2026-06-07)

Fixes from the first real drain run (2 failed, 3 staged — one a clean extraction wrongly held back).

- **Bug — `'list' object has no attribute 'get'`.** Multi-item text (e.g. "protein shake + 300g
  grilled fish + 1 bowl rice") makes the model return a **list** of food dicts, but the write path
  called `.get()` on it as a single dict. `_process_cluster`'s food branch now normalises to a list,
  calls `maintainer_ingest_food` once per item, and marks the cluster once with an aggregate status
  (any failed → failed; else any staged → staged; else done).
- **Bug — empty / non-JSON vision response crashed → failed.** `vision_extract` now returns
  `{"confidence": 0.0, "_stage_reason": "vision returned no parseable extraction"}` on
  `JSONDecodeError | KeyError | IndexError` (malformed or empty body). `_process_cluster` treats
  `_stage_reason` as a **fail-safe stage**, not a hard fail. HTTP/network errors still return
  `_error` → failed (unchanged).
- **Confidence gating → completeness-based (Bug 3).** Self-reported model confidence is unreliable
  (a full-macro protein shake scored 0.5 and was wrongly staged). New `food_is_complete(extracted)`:
  auto-write requires `description AND calories AND ≥1 macro (protein_g|carbs_g|fat_g)`; otherwise
  stage. Confidence is still **recorded** (`p_confidence`), no longer the primary gate. A second
  plausibility gate on calories runs DB-side in the new RPC against `food_energy_kcal` bounds
  `[25, 12000]` in `metric_definitions` — the "1,020 kcal → 20" comma-slip backstop (20 < 25 → staged).
- **Migration 036 — `maintainer_ingest_food` adds `p_force_stage boolean DEFAULT FALSE`.**
  DROP + CREATE (param-list change can't use CREATE OR REPLACE). Python's completeness gate sets
  `p_force_stage`; the RPC routes to `stg_food_log_review` when forced or when calories fall outside
  the plausibility bound, else to `food_logs`. Auth guard (`is_maintainer() AND has_profile_access()`)
  copied verbatim — routing changed, not auth. Re-applies full grant posture (`REVOKE FROM PUBLIC`
  + `REVOKE FROM anon` + `GRANT TO authenticated`) since a fresh CREATE resets to PUBLIC EXECUTE.
  `NOTIFY pgrst, 'reload schema'` in the tail so PostgREST picks up the new signature.
- **Collapsed duplicate config keys.** `ingest.confidence_min` removed from `system_config`
  (migration 036); `lib/contract.confidence_min()` now reads the single canonical
  `ingest.confidence_threshold`. Both held the same value; readers in `ingest/{food,biomarker,supplement}.py`
  are unaffected (they call `confidence_min()`, which now points at the surviving key).
- **Tests (`tests/unit/test_inbox_drain.py`).** `food_is_complete` unit cases (0.5-confidence
  shake → True, bare caption / calories-only / no-description → False); completeness gate asserts the
  actual `p_force_stage` value sent to the RPC (shake → False/written, bare caption → True/staged);
  list-of-foods write path (3 RPCs, 1 mark, all-inserted vs mixed-status); empty-vision → staged not
  failed; unknown/workout kind → force_stage=True (never auto-writes to prod); `write_food` forwards
  `force_stage`; `vision_extract` parse-error/empty-body → `_stage_reason`. The two former
  confidence-routing tests were re-pointed to assert the sent `p_force_stage` (they had become
  circular — the mock returned the status regardless of routing). 36 pass in the file; full unit
  suite 179 passed / 9 skipped.

**Deployed 2026-06-07:** migration 036 applied. Live DB verified — single 15-param
`maintainer_ingest_food` (old signature dropped), grants `authenticated`/`postgres`/`service_role`
only (no `anon`, no `PUBLIC`), `ingest.confidence_min` removed (`ingest.confidence_threshold` 0.7
remains), `food_energy_kcal` bound `[25, 12000]` active.

## v3.6.0 — Telegram ingestion Phase 3B: autonomous drain with vision extraction (2026-06-07)

- **Migration 032 — drain service identity + lock down ingest RPCs.**
  - `family_memberships` rows for `healthspan.drain@chitalkar.com` (UID
    `1e2084cf-b1c4-484b-8448-33750c4d8e6c`) → PC, Dea, Dev profiles as `owner`.
    Gives drain service `is_maintainer()` = true and `has_profile_access()` = true for all
    three profiles, satisfying SECURITY DEFINER RPC guards.
  - `REVOKE EXECUTE FROM anon` on all three RPCs (Supabase assigns anon grants explicitly;
    `REVOKE FROM PUBLIC` in migration 031 was insufficient). Verified anon grant count = 0.
- **Migration 033 — drain identity re-key after account recreation.**
  - `healthspan.drain@chitalkar.com` deleted + recreated → new UID
    `06ada8f7-76d7-49bf-90ef-93a9103b8b11`. `ON DELETE CASCADE` removed migration 032 rows.
    Re-inserted 3 `family_memberships` rows with new UID.
- **Migration 034 — drain vision model config.**
  - `drain.vision_model = "claude-sonnet-4-6"` in `system_config`. Read by `inbox_drain`
    at runtime; no hardcoded model names.
- **`monitor/inbox_drain.py` — fully autonomous `--once` entrypoint (Phase 3B rewrite).**
  - New: `vision_extract(api_key, model, image_blocks, caption, kind, now_iso) → dict` — POSTs
    to `https://api.anthropic.com/v1/messages` using `HS_ANTHROPIC_API_KEY` (platform reserves
    the plain `ANTHROPIC_API_KEY`). Reads model from `drain.vision_model` system_config.
    Returns parsed JSON or `{"confidence": 0.0, "_error": "..."}` on failure — always routes
    to staging rather than crashing the drain.
  - New: `content_cluster_ungrouped(api_key, model, items) → list[list[dict]]` — text-only
    API call groups ungrouped inbox items by caption/kind similarity. Single item skips API
    call. Falls back to singletons on any error.
  - New: `get_signed_url(db, storage_path)`, `_download_image(url)`, `_image_block(bytes, ct)` —
    Storage access pipeline. `storage_path=None` items skip gracefully.
  - New: `telegram_send(token, chat_id, text)` — best-effort, never raises.
  - New: `compose_confirmation(kind, rpc_status, extracted, is_minor) → str` — minor-safe:
    no deficit/restriction language when `is_minor=True`; growth/performance framing only.
  - New: `_process_cluster(...)` — complete single-cluster pipeline: download → vision →
    UUID lookup → write RPC → mark rows → Telegram confirm. Updates summary dict in place.
  - New: `run_once(db, cfg, api_key, token) → dict` — full drain loop. Returns
    `{fetched, clustered, written, staged, failed, errors:[]}`.
  - New: `__main__` with `argparse --once`: password-grant sign-in, runs `run_once`, prints
    JSON summary to stdout, exits 0 (no failures) or 1 (any failure).
- **`docs/ROUTINE-PROMPT.md` — replaced with minimal imperative version.**
  - Previous: 9-step Routine orchestration guide for an LLM Routine to call.
  - Now: 3-line RUN-MODE prompt — `python -m monitor.inbox_drain --once`, report JSON,
    stop on error. No codebase exploration, no file edits, no architectural reasoning.
- **`tests/unit/test_inbox_drain.py` — 21 tests (expanded from 17). All pass.**
  - Added: `test_vision_extract_food_parsed`, `test_vision_extract_parse_error_returns_error_dict`,
    `test_vision_extract_api_error_returns_error_dict`, `test_image_block_shape`.
  - Added: `test_content_cluster_api_groups_two`, `test_content_cluster_api_keeps_separate`,
    `test_content_cluster_api_failure_falls_back_to_singletons`.
  - Added: `test_run_once_written`, `test_run_once_staged`, `test_run_once_failed_on_vision_error`,
    `test_run_once_empty_inbox`.
  - Mock strategy: `_mock_http_resp(body)` helper for httpx calls (MagicMock-based, avoids
    Python 3.14 `httpx.Response(json=...)` unreliability). `_db()` uses
    `content=json.dumps(body).encode()` + explicit content-type header.
  - Key fix: `run_once` receives `cfg` directly — first DB call is `telegram_identities`,
    not `get_config`. Mock response sequences corrected accordingly.

## v3.5.0 — Telegram ingestion Phase 3A: cloud drain + maintainer-ingest write path (2026-06-07)

- **Migration 031 — media_group_id + system_config seeds + 3 SECURITY DEFINER RPCs.**
  - `media_inbox.media_group_id text NULL` — Telegram album burst identifier. Rows sharing this
    value are drained as one cluster (one extraction → one log entry). Commented for SCHEMA-MAP.
  - 4 new `system_config` rows: `push.inbox_settle_sec=90` (settle delay before drain picks up
    an item, ensuring album bursts arrive fully), `ingest.confidence_threshold=0.7` (LLM confidence
    gate for prod vs staging), `routine.last_fire_at` (epoch initial value, CAS dedup key for
    Routine fires), `routine.fire_dedup_sec=300` (min seconds between consecutive Routine fires).
  - `maintainer_ingest_food(p_profile_id, …)` — SECURITY DEFINER, SEARCH_PATH=public. Guard:
    `is_maintainer() AND has_profile_access(p_profile_id)` (verified: PC satisfies both for Dea).
    High confidence → `food_logs` plain INSERT (no ON CONFLICT — Telegram entries have no
    `source_log_path`, partial unique index does not apply; idempotency via atomic inbox claim).
    Low confidence → `stg_food_log_review`. Returns `{id, status: "inserted"|"staged"}`.
  - `maintainer_ingest_biomarker(p_profile_id, …)` — Upserts on `(profile_id, metric_definition_id,
    measured_at)`. Staging to `stg_biomarker_review` on low confidence.
  - `maintainer_ingest_supplement(p_profile_id, …)` — Upserts on `(profile_id, supplement_id,
    taken_on, source)`. `taken_on` is a GENERATED column — never written directly; computed from
    `taken_at AT TIME ZONE 'UTC'`. Staging to `stg_supplement_intake_review` on low confidence.
  - All three: `REVOKE EXECUTE FROM PUBLIC; GRANT EXECUTE TO authenticated`.
  - Verify DO block asserts: column present, 3 RPCs found, 4 config rows exist.
- **`lib/db_rest.py` — httpx PostgREST client for cloud Routines (NEW).**
  - Auth: `sign_in(url, anon_key, email, password)` → password-grant JWT. No service_role.
  - `DbRest`: `select / insert / update / rpc / claim_inbox_item`. Context-manager.
  - `claim_inbox_item(id)`: PATCH with `status=eq.pending` filter + `Prefer: return=representation`.
    Returns True only if ≥ 1 row returned — the only caller that wins the atomic update.
  - Cloud constraint: no psycopg2 (TCP 6543 blocked) and no supabase-py (cffi/cryptography
    native module fails). Raw HTTPS via httpx is the only viable path.
- **`monitor/inbox_drain.py` — Routine drain logic (NEW).**
  - `fetch_settled(db, settle_sec)`: pending items older than `push.inbox_settle_sec`.
  - `claim(db, item_id)`: delegates to `db_rest.claim_inbox_item`.
  - `build_clusters(items)`: album clusters (shared `media_group_id` ≥ 2 rows) + ungrouped.
    Singletons and no-group items go to ungrouped for Routine LLM content-clustering.
  - `merge_caption(rows)`: deduplicates captions across a cluster.
  - `write_food / write_biomarker / write_supplement`: call SECURITY DEFINER RPC, mark all
    cluster rows done/staged atomically. RPC error → rows marked `failed`.
  - `mark_rows(db, ids, status, result_ref)`: bulk PATCH with `id=in.(…)` filter.
  - `lookup_metric / lookup_supplement_by_name`: REST ilike for name→UUID resolution.
  - CLI: `python -m monitor.inbox_drain` — prints cluster summary.
- **`supabase/functions/telegram-webhook/index.ts` — Phase 3A additions.**
  - Captures `msg.media_group_id` → `media_inbox.media_group_id` on every enqueue.
  - `maybeFireRoutine(db)`: atomic CAS PATCH on `system_config.routine.last_fire_at.updated_at`
    (only updates if `updated_at < now - fire_dedup_sec`). Winner POSTs to `ROUTINE_TRIGGER_URL`
    (env secret). Loser skips silently. Wrapped in `EdgeRuntime.waitUntil` — never delays 200.
    `ROUTINE_BEARER` optional auth header for trigger endpoint.
- **`docs/ROUTINE-PROMPT.md` — self-contained Routine orchestration prompt (NEW).**
  - Covers: all 9 steps (setup → fetch → claim → signed URL → extraction → UUID resolution →
    write/stage → content-clustering → confirmation). Extraction prompts for food / lab / supplement.
  - Error handling table. Minor-safe framing rules. Idempotency guarantees documented.
  - Ready to paste as Routine system prompt once `ROUTINE_TRIGGER_URL` is wired.
- **`tests/unit/test_inbox_drain.py` — 17 unit tests (NEW). All pass.**
  - `httpx.MockTransport` — no live DB or HTTP. Tests: claim won/lost, select/insert/rpc,
    build_clusters (album/singleton/two-unrelated/mixed), merge_caption, write_food
    (high-confidence → inserted, low-confidence → staged), minor-framing assertions (3 scenarios),
    get_config dict shape.
- **`docs/CODE-MANIFEST.html` — updated for Phase 3A.** New cards: migration 031,
  lib/db_rest.py, monitor/inbox_drain.py, test_inbox_drain.py, ROUTINE-PROMPT.md.
  Threshold table extended with 4 Phase 3A `system_config` keys. Open items updated.
- **Deployed:** `telegram-webhook` redeployed with media_group_id + fire-trigger
  (64.97 kB). `whoop-webhook` no-change bundle confirmed.

## v3.4.1 — whoop-webhook: EdgeRuntime.waitUntil for push background task (2026-06-07)

- **`supabase/functions/whoop-webhook/index.ts`** — reliability fix for Phase 2 push block.
  - The bare fire-and-forget `(async () => {...})()` could be terminated when the Supabase
    Edge Function isolate is reclaimed after the 200 response, silently dropping all pushes
    and the dead-man heartbeat.
  - Fix: capture the promise as `pushTask`, then register it with `EdgeRuntime.waitUntil(pushTask)`.
    The runtime keeps the isolate alive until the promise settles without delaying the 200.
  - typeof-guard fallback: `if (typeof EdgeRuntime !== "undefined") ... else await pushTask` —
    local/test environments (where `EdgeRuntime` is undefined) await the promise directly
    instead of silently no-oping.
  - No DB change, no migration. Inner try/catch unchanged — push errors remain non-fatal.
- **Deployed:** `whoop-webhook` redeployed to dsnydskkjwziynwmzfkh (72.43 kB).
  `telegram-webhook` redeployed (no-change bundle confirmed).

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
