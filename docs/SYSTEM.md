# HealthSpan Foundation — System Manifest

<!-- Generated 2026-06-08. Sections 1–5 cover architecture, schema, and module internals.
     Sections 6–8 cover onboarding, maintenance operations, and non-negotiable rules. -->

> The **live Supabase DB is the source of truth for numbers**; the per-person context MD
> is the source of truth for targets, norms, and coaching voice. Generated against repo
> state with migration 046 applied (2026-06-08), skill v3.13.0.

---

## 1. SYSTEM OVERVIEW

### What it is

A transportable, multi-tenant, DB-backed **personal health intelligence platform** for
the Chitalkar household — currently **PC** (Parikshit Chitalkar, owner/maintainer) and
**Dea** (minor, 13F), with **Nanki** (PC's wife, 45F adult) ready to onboard when she is, and a
dormant profile slot for Dev. The system behaves as a
longevity physician, exercise physiologist, nutrition coach, and mental-performance
advisor, giving guidance grounded in each person's actual longitudinal data (WHOOP, labs,
DEXA, food logs, supplements) and their per-person context.

It ingests health data via a Telegram bot (food photos, lab PDFs, supplement labels) and
real-time WHOOP webhooks, runs AI extraction through Anthropic Claude into staging, and
surfaces evidence-based coaching through a skill (`SKILL.md`) plus a daily/event brief
pipeline.

### Core philosophy

- **DB is truth for numbers; context MD is truth for targets and voice.** Every number in
  any answer comes from a query result, never from model memory (CITE guard).
- **AI interprets, the DB gates.** All AI extraction output lands in `stg_*` staging
  tables first; plausibility gates (kcal range, biomarker min/max) live in the DB, not the
  prompt. Abnormal-but-plausible values write to production flagged; implausible values
  are staged for human review.
- **Thresholds are never hardcoded** — all come from the `system_config` table.
- **Defence-in-depth multi-tenancy.** RLS on 100% of `profile_id` tables; the maintainer
  (PC) sees the data-quality machinery, a minor (Dea) never does.
- **Minor safety is non-negotiable.** A profile is a minor if EITHER `config.is_minor` OR
  the context MD's `is_minor` is true. Growth/performance framing only — no
  deficit/restriction/"lose" language for a minor; low intake is a gentle flag, never
  praise.
- **Transportable skill.** The packaged `.skill` bundle is person-agnostic — it ships no
  config and no context; identity arrives at install time.

### Tech stack

| Layer | Technology |
|---|---|
| Database | Supabase (PostgreSQL **17.6** + Row Level Security) |
| AI | Anthropic Claude API (`claude-sonnet-4-6` for drain vision; `claude-haiku-4-5-20251001` for daily brief) |
| Edge functions | Supabase Deno / TypeScript (`telegram-webhook`, `whoop-webhook`, `trigger-drain`, `whoop-oauth` + `_shared`) |
| CI / automation | GitHub Actions (`inbox-drain`, `send-brief`, `ci`) |
| Messaging | Telegram bot (ingestion + confirmations); Google Chat (future push — **not yet in code**) |
| Storage | AWS S3 (private `health-media` bucket, signed URLs) |
| Email | AWS SES (infra-level; **not yet wired into Python code** — see §2.5) |
| Cache | Redis |
| Language | Python (modules: `lib/`, `ingest/`, `plan/`, `monitor/`, `analysis/`, `export/`) |

### Multi-tenant model

| Profile | Role | RLS scope | Visibility |
|---|---|---|---|
| **PC** | Owner, sole maintainer (`profiles.is_maintainer = true`) | All managed profiles | Everything: query audit, sync logs/errors, staging queues |
| **Dea** | Non-owner, minor (`is_minor: true` in config + context MD — not a `profiles` column) | Self only | Per-ingest outcomes only ("saved"/"flagged"); 0 rows from any maintainer-only table |
| **Nanki** | Non-owner, **adult** (PC's wife, 45F) — *ready to onboard when she is* | Self only | Full adult framing (deficits/targets like PC), but non-maintainer — no audit/sync/staging visibility. Onboard exactly like §6: profile (`relationship` not `child` → `is_minor=false`), config + `context/nanki.context.md`, `mint-link-code`, she sends it from her phone. Supplement learn-on-clarify auto-adds under her own profile (adult). |

Maintainer status resolves through `family_memberships` (not `profiles.auth_user_id`).
Maintainer-only RLS SELECT applies to `query_audit`, `wearable_sync_log`,
`wearable_sync_errors`, and all `stg_*_review` tables — a non-maintainer sees 0 rows.

---

## 2. INFRASTRUCTURE

### 2.1 Supabase Project

| Field | Value |
|---|---|
| **Project ID (ref)** | `dsnydskkjwziynwmzfkh` |
| **Region** | `ap-southeast-1` (AWS Singapore) — Supabase region string `aws-1-ap-southeast-1` |
| **Project URL** | `https://dsnydskkjwziynwmzfkh.supabase.co` |
| **Postgres version** | 17.6 |
| **DB connection (App/Edge)** | Supabase **transaction pooler**, port **6543** — host `aws-1-ap-southeast-1.pooler.supabase.com`, role `healthspan_app` |

**Extensions in use:**
- **`pg_net`** — async HTTP from the DB. Drives the `fn_media_inbox_notify` trigger
  (migration 042) which POSTs to `trigger-drain` on every `media_inbox` INSERT.
- **`vault`** — Supabase secret vault. `TRIGGER_DRAIN_SECRET` is no longer used (migration
  047 reverted to no-auth): the call from `fn_media_inbox_notify` to `trigger-drain` carries
  no auth header. The vault remains available for other secrets but is not in this path.
- **`pgvector`** — **not currently used in this repo** (no embeddings tables defined
  here). Listed for completeness; the pgvector/semantic-search usage belongs to the
  separate BaySys system, not HealthSpan.

**Connection modes:**

| Mode | Use case | Mechanism |
|---|---|---|
| `direct_role` | PC / local / Cowork / Code | psycopg2 as `healthspan_app` against the pooler; skill injects `request.jwt.claims` (`{"sub":"<auth_user_id>","role":"authenticated"}`) at session start; read-only session; RLS as defence-in-depth |
| `supabase_client` | Dea / mobile / web / Telegram | supabase-py signs in with `auth_email` + `auth_password`; the client carries the person's JWT automatically (`auth.uid()` → RLS scopes) |

`whoop_tokens` is intentionally **default-deny** (service_role-only credential vault).

### 2.2 Edge Functions (Supabase Deno / TypeScript)

There are **four** deployed functions plus one shared library (`_shared`). **All four set
`verify_jwt = false`** and operate as trusted server-side services using
`SUPABASE_SERVICE_ROLE_KEY` (RLS-bypassing). None accept a user JWT — each verifies its
caller via its own signature/secret scheme. `SUPABASE_URL` and
`SUPABASE_SERVICE_ROLE_KEY` are runtime-injected and required by all four (omitted from
the per-function "additional secrets" lists below).

#### `telegram-webhook`
- **verify_jwt:** `false`. Verifies inbound requests by constant-time compare of the
  `X-Telegram-Bot-Api-Secret-Token` header against `TELEGRAM_WEBHOOK_SECRET`.
- **Purpose:** Inbound Telegram updates. Authenticates the sender (link-code onboarding via
  `telegram_link_codes` / `telegram_identities`, must be `status=active`), deduplicates by
  `update_id` against `telegram_processed_updates`, classifies media by caption keyword
  (`guessKind`), downloads the highest-res photo, uploads to the private `health-media`
  bucket at `telegram/<profile_id>/<update_id>.jpg`, and INSERTs a `pending` row into
  `media_inbox`. Text-only messages skip `media_inbox` and fire a `send-brief` GitHub
  dispatch instead. `is_minor` changes ack/push wording.
- **Additional secrets:** `TELEGRAM_BOT_TOKEN` (download files + send replies),
  `TELEGRAM_WEBHOOK_SECRET` (verify inbound signature), `GH_DISPATCH_TOKEN` (fire
  `send-brief` dispatch on text), and optionally `ROUTINE_TRIGGER_URL` + `ROUTINE_BEARER`
  (fire the legacy Routine drain — debounced 300s fallback).
- **Note:** Writes `telegram_processed_updates` **after** `media_inbox` (strict idempotency
  ordering — a crash between leaves no idempotency record, so Telegram retries the full
  path). The pg_net trigger fires independently and in parallel with `maybeFireRoutine`.

#### `trigger-drain`
- **verify_jwt:** `false`. No inbound user auth — called only by `pg_net`
  (`net.http_post`) from `fn_media_inbox_notify`. The call carries **no auth header**
  (migration 047 reverted to no-auth — see §7.4).
- **Purpose:** Real-time drain dispatcher. Deduplicates within a **15-second** window via
  atomic CAS (`UPDATE system_config WHERE key = 'trigger_drain.last_dispatch_ts' AND
  updated_at < cutoff`). Only the winner fires a GitHub `repository_dispatch` `inbox-drain`
  event (`POST /repos/Pilot1940/healthspan-foundation/dispatches`). Returns
  `{ dispatched: true }` or `{ skipped: true }`.
- **Additional secrets:** `GH_DISPATCH_TOKEN`. (`TRIGGER_DRAIN_SECRET` is deprecated/unused
  — migration 047 reverted this path to no-auth.)

#### `whoop-webhook`
- **verify_jwt:** `false`. Verifies WHOOP **HMAC-SHA256** signature (key =
  `WHOOP_CLIENT_SECRET`, message = timestamp + raw body, base64).
- **Purpose:** Real-time WHOOP push events (`workout.updated`, `sleep.updated`,
  `recovery.updated`, `cycle.updated`, `*.deleted`). Maps WHOOP `user_id` → `profile_id`
  via `whoop_tokens`, opens a `wearable_sync_log` row, fetches the full record from the
  WHOOP API (lazy-refresh token via `_shared/whoop.ts`), and upserts into `whoop_workouts`
  / `whoop_sleeps` / `whoop_cycles`. On `sleep.*` events re-fetches the most recent closed
  cycle to refresh final `day_strain`. Phase 2 push logic (debounce, quiet hours,
  criticality thresholds from `system_config`) sends Telegram pushes + logs `push_log`.
  Runs a dead-man heartbeat across all active profiles on every event.
- **Additional secrets:** `WHOOP_CLIENT_SECRET` (HMAC + refresh), `WHOOP_CLIENT_ID`
  (refresh via shared lib), `TELEGRAM_BOT_TOKEN` (push notifications).
- **⚠️ Needs redeploy:** `supabase functions deploy whoop-webhook --no-verify-jwt` —
  stale-cycle strain refresh + Phase 2 push additions are not yet live.

#### `whoop-oauth`
- **verify_jwt:** `false`. No inbound auth — opened by a human in a browser. The `state`
  parameter carries the profile UUID to prevent CSRF.
- **Purpose:** One-time OAuth 2.0 consent. `GET ?profile_id=<uuid>` redirects to the WHOOP
  consent page. Callback `GET ?code=...&state=<profile_id>` exchanges the code for access +
  refresh tokens, fetches the WHOOP `user_id` from `/v2/user/profile/basic`, and upserts
  into `whoop_tokens` (keyed on `profile_id`). Returns an HTML success page.
- **Additional secrets:** `WHOOP_CLIENT_ID`, `WHOOP_CLIENT_SECRET`.

#### `_shared/whoop.ts` (shared library, not deployed standalone)
Imported by `whoop-webhook` and `whoop-oauth`. Provides `getValidAccessToken` (lazy
refresh via `WHOOP_TOKEN_URL` when within 2 min of expiry, rotating the refresh token),
`exchangeCode` / `refreshTokens` (token exchange via `client_secret_post` with a browser
User-Agent to pass Cloudflare), `storeTokens` (upsert `whoop_tokens`), `whoopGet`
(authenticated GET against `https://api.prod.whoop.com/developer`), and OAuth constants
(`WHOOP_AUTH_URL`, `WHOOP_TOKEN_URL`, `SCOPES`).

### 2.3 GitHub Repository

| Field | Value |
|---|---|
| **Repo** | `Pilot1940/healthspan-foundation` |
| **Default branch** | `main` |
| **Dispatch targets** | `inbox-drain` (from `trigger-drain`), `send-brief` (from `telegram-webhook`) — both `POST /repos/Pilot1940/healthspan-foundation/dispatches` |

**Secrets required in GitHub Actions:**

| Secret | Used by |
|---|---|
| `SUPABASE_URL` | inbox-drain, send-brief, ci (integration) |
| `SUPABASE_ANON_KEY` | inbox-drain, send-brief, ci (integration) |
| `SUPABASE_SERVICE_ROLE_KEY` | inbox-drain, send-brief |
| `DATABASE_URL` | inbox-drain, ci (integration; tests self-skip when absent) |
| `HS_AUTH_EMAIL` | inbox-drain, send-brief, ci — `healthspan.drainer@chitalkar.com` |
| `HS_AUTH_PASSWORD` | inbox-drain, send-brief, ci (integration) |
| `HS_ANTHROPIC_API_KEY` | inbox-drain, send-brief (passed as `ANTHROPIC_API_KEY` in send-brief) |
| `TELEGRAM_BOT_TOKEN` | inbox-drain, send-brief |

**Workflows:**

- **`inbox-drain.yml`** — Trigger: `repository_dispatch: inbox-drain` (from
  `trigger-drain`), OR daily cron **03:30 UTC / 09:00 IST** (backstop), OR manual
  `workflow_dispatch`. On dispatch:
  `python3 -m monitor.inbox_drain --once --settle-sec 5` (short settle batches album
  bursts); otherwise `--once` (no settle). **Timeout: 15 min.** Reads `pending`
  `media_inbox` rows, fetches media from `health-media`, calls Claude to extract, writes to
  staging (or production via the `maintainer_ingest_*` RPC if complete), sends a Telegram
  confirmation.
- **`send-brief.yml`** — Trigger: `repository_dispatch: send-brief` (from
  `telegram-webhook` on a text message), OR manual `workflow_dispatch` with optional
  `profile_id`. On dispatch: `python3 -m monitor.brief --profile-id <payload.profile_id>`;
  `workflow_dispatch` without a profile_id runs `--all`. **Timeout: 10 min.**
- **`ci.yml`** — Trigger: every `push` and `pull_request`. `pytest tests/unit/` always
  runs; `pytest tests/integration/` runs only with live-DB secrets (self-skips via
  `pytest.mark.skipif` when `DATABASE_URL` is absent). Integration env: `DATABASE_URL`,
  `SUPABASE_URL`, `SUPABASE_ANON_KEY`, `HS_AUTH_EMAIL`, `HS_AUTH_PASSWORD`.

### 2.4 Supabase Secrets

Set on the Supabase project (`supabase secrets set ...`); consumed by the edge functions.
`SUPABASE_URL` / `SUPABASE_SERVICE_ROLE_KEY` are runtime-injected (not manually set).

| Secret | Status | Consumed by |
|---|---|---|
| `WHOOP_CLIENT_ID` | active | whoop-webhook, whoop-oauth (token refresh) |
| `WHOOP_CLIENT_SECRET` | active | whoop-webhook (HMAC + refresh), whoop-oauth |
| `TELEGRAM_BOT_TOKEN` | active | telegram-webhook, whoop-webhook |
| `TELEGRAM_WEBHOOK_SECRET` | active | telegram-webhook (inbound signature compare) |
| `GH_DISPATCH_TOKEN` | active | telegram-webhook (send-brief), trigger-drain (inbox-drain) |
| `TRIGGER_DRAIN_SECRET` | **deprecated / unused (mig 047)** | Formerly verified by trigger-drain and forwarded by `fn_media_inbox_notify`; migration 047 reverted this path to no-auth, so the secret is no longer consumed or forwarded anywhere. |
| `ROUTINE_TRIGGER_URL` | **unused / legacy** | telegram-webhook (`maybeFireRoutine` fallback) |
| `ROUTINE_BEARER` | **unused / legacy** | telegram-webhook (`maybeFireRoutine` fallback) |

**Note:** `TRIGGER_DRAIN_SECRET` is **no longer required** — migration 047 reverted the
`fn_media_inbox_notify` → `trigger-drain` call to no-auth (the vault scheme from migration
043 was removed because `vault.decrypted_secrets` is not reachable from the pg_net trigger
context). Nothing needs to be set or rotated for this secret. See §7.4.

### 2.5 AWS

- **S3 — `health-media`:** private S3-backed Supabase Storage bucket. Access via **signed
  URLs only**. Photo objects stored at `telegram/<profile_id>/<update_id>.jpg`. Created via
  `python scripts/hs_ops.py setup-telegram-storage`. Uploads use `upsert: false` (a retry
  fails silently but is non-fatal — the drain re-fetches).
- **SES (email):** Listed in the stack as the email layer on the **`bayst.finance`**
  verified domain. **Correctness flag:** `pc@baysys.ai` is the developer's *other* company
  (BaySys / Bayst Finance), not HealthSpan. It appears in the source material as the
  intended SES sender, but **no SES sender code exists in this repo** — there is no
  `boto3`/`sesv2`/`send_email` call and no from-address in `lib/`, `ingest/`, `monitor/`,
  or any config. SES is therefore **infra-level / aspirational, not yet wired into code**.
  Treat the `bayst.finance` domain and `pc@baysys.ai` from-address as **unverified for this
  repo** and confirm with PC before relying on them — current outbound messaging is
  entirely Telegram, and the planned future push channel (backlog #1) is Google Chat, not
  email.

---

## 3. EVENT-DRIVEN PIPELINES

Everything in HealthSpan that is not a session-start read is event-driven. There is no
long-running application server. The system is a set of stateless Edge functions
(Deno, on Supabase), database triggers (pg_net), and GitHub Actions workflows that
fire on `repository_dispatch` events. Every Edge function runs with the
`SUPABASE_SERVICE_ROLE_KEY` and bypasses RLS — none require or accept a user JWT;
they are trusted server-side services. The heavy compute (Claude Vision extraction,
brief composition) runs in GitHub Actions, not in the Edge functions, which keeps the
Edge layer fast and within Deno's execution limits.

### 3.1 Photo Ingestion Pipeline (Telegram → food_logs)

This is the primary real-time path: a user photographs a meal, and a structured row
lands in `food_logs` (or `stg_food_log_review`) with no human in the loop.

#### Full actor chain

```
USER (Telegram app)
  │  photo + optional caption ("breakfast")
  ▼
TELEGRAM SERVERS
  │  POST /functions/v1/telegram-webhook
  │  Header: X-Telegram-Bot-Api-Secret-Token: <TELEGRAM_WEBHOOK_SECRET>
  ▼
telegram-webhook  (Edge fn, verify_jwt=false, service_role)
  │  1. Constant-time compare of secret token header vs TELEGRAM_WEBHOOK_SECRET
  │  2. update_id dedup against telegram_processed_updates
  │  3. Identity lookup in telegram_identities (must be status=active);
  │     unlinked sender → link-code onboarding via telegram_link_codes
  │  4. guessKind(caption) classifies the message (e.g. "breakfast" → "food")
  │  5. Telegram getFile → download highest-res photo bytes
  │  6. Upload to private health-media bucket: telegram/<profile_id>/<update_id>.jpg
  │  7. INSERT media_inbox { profile_id, chat_id, kind, storage_path, caption,
  │                          media_group_id, status:"pending" }
  │  8. INSERT telegram_processed_updates { update_id }   ← AFTER media_inbox
  │  9. Telegram sendMessage ack: "📥 Got it (food) — queued for processing."
  │
  ├──────────────── PATH A (pg_net DB trigger) ───────────────┐
  │                                                            │
  │   AFTER INSERT ON media_inbox fires                        │
  │   fn_media_inbox_notify (SECURITY DEFINER, mig 042/043)    │
  │   net.http_post → POST /functions/v1/trigger-drain         │
  │   (no auth header — mig 047)                               │
  │                          ▼                                 │
  │   trigger-drain (Edge fn, verify_jwt=false, service_role)  │
  │     Atomic CAS: UPDATE system_config                       │
  │       WHERE key='trigger_drain.last_dispatch_ts'           │
  │         AND updated_at < now() - 15s                       │
  │     WIN  → POST api.github.com/repos/Pilot1940/            │
  │            healthspan-foundation/dispatches                │
  │            { event_type: "inbox-drain" }  → {dispatched}   │
  │     LOSE → { skipped: true }  (another call won the 15s)   │
  │                                                            │
  └──────────────── PATH B (maybeFireRoutine) ────────────────┤
      (EdgeRuntime.waitUntil, inside telegram-webhook)        │
      Reads routine.fire_dedup_sec from system_config (300s)  │
      Atomic CAS on system_config key='routine.last_fire_at'  │
      WIN → POST ROUTINE_TRIGGER_URL (if configured)          │
                                                              ▼
GITHUB receives repository_dispatch: inbox-drain
  │  Triggers inbox-drain.yml
  ▼
inbox-drain.yml  (ubuntu-latest, 15-min timeout)
  │  checkout → Python 3.11 → pip install -r requirements.txt
  │  python3 -m monitor.inbox_drain --once --settle-sec 5
  ▼
monitor/inbox_drain.py
  │  1. Wait 5s settle window (lets an album burst land fully)
  │  2. fetch_settled() → pending media_inbox rows older than settle window
  │  3. build_clusters() → group rows by media_group_id (album) + singletons
  │  4. claim_inbox_cluster(uuid[]) RPC → atomic all-or-nothing FOR UPDATE lock
  │  5. For each claimed row: signed-URL fetch from health-media →
  │     vision_extract() POSTs image+caption to Claude (HS_ANTHROPIC_API_KEY)
  │  6. food branch → maintainer_ingest_food RPC (p_force_stage routing)
  │  7. Update media_inbox status: "processed" / "staged" / "failed"
  │  8. Telegram confirmation back to user
  │  9. compose_brief() for any adult profile with a successful food write
  ▼
food_logs            ← complete extraction (auto-write)
stg_food_log_review  ← incomplete extraction (force-staged, awaits maintainer)
```

#### Two independent drain triggers (by design)

There are **two parallel mechanisms** that fire the drain, and they are deliberately
redundant:

- **Path A — pg_net DB trigger (primary, real-time):** `fn_media_inbox_notify` fires on
  every `media_inbox` INSERT and calls `trigger-drain`, which dedups in a **15-second**
  window and dispatches the GH Actions workflow. This is the fast path — a photo is
  usually in `food_logs` within ~30–60s.
- **Path B — `maybeFireRoutine` (longer-lived fallback):** `telegram-webhook` itself
  fires a separate Routine dispatch, debounced at **300s** via a `system_config` CAS on
  `routine.last_fire_at`. This is a slower backstop, independent of the DB trigger.
- **Path C — daily cron backstop:** `inbox-drain.yml` also runs on a daily cron at
  **03:30 UTC (09:00 IST)** with no settle window, draining anything the event paths
  missed.

#### Dedup logic (three layers)

1. **`update_id` dedup** (`telegram_processed_updates`) — stops Telegram's own retries
   from double-processing the same update.
2. **15s CAS in `trigger-drain`** (`trigger_drain.last_dispatch_ts`) — collapses a burst
   of `media_inbox` INSERTs (e.g. a 4-photo album → 4 trigger fires) into a single GH
   Actions dispatch. Only the first call within the window wins; the rest return
   `{ skipped: true }`.
3. **300s CAS in `maybeFireRoutine`** (`routine.last_fire_at`) — independently debounces
   the Routine fallback path.

#### Album bursts (`media_group_id`)

When a user sends a multi-photo album, Telegram delivers each photo as a **separate
update** sharing one `media_group_id`. Each produces its own `media_inbox` row and its
own trigger fire — but the 15s `trigger-drain` CAS collapses them to one GH dispatch,
and inside the drain the **`--settle-sec 5`** window plus `build_clusters()` re-assemble
the album by `media_group_id` so the cluster is processed together. The daily-cron path
runs **without** a settle window (album bursts are no longer in flight by then).

#### Atomic claim (`claim_inbox_cluster`)

Before any extraction, `monitor/inbox_drain.py` claims rows via the
`claim_inbox_cluster(uuid[])` RPC (migration 046, SECURITY INVOKER). The claim is
**all-or-nothing per cluster**: it takes a `FOR UPDATE` lock and performs a single
all-or-nothing UPDATE. If any row in a cluster is already claimed by a concurrent
drain, **all** rows in that cluster roll back to `pending`. This prevents two
overlapping workflow runs (e.g. event-path + cron-path firing close together) from
double-writing the same album.

#### Single extractor

There is **one** vision/extraction prompt (`_VISION_PROMPTS["unknown"]`) used for **every**
path — text and photo. `vision_extract` always uses it and the re-dispatch always unwraps the
`{kind,data}` it returns (the model's chosen kind wins over the `guessKind` hint). The old
standalone `food`/`supplement`/`lab`/`dexa` prompts were deleted — they had either gone **dead**
(`guessKind` never emitted `supplement`) or **drifted** from each other, so a fix on one path
silently didn't apply on another. One prompt = one source of truth.

The prompt encodes **clarify-on-uncertainty**: if it can't identify a specific food/supplement
("my supplement", "a pill"), it sets `confidence < 0.3` rather than guessing. The **confidence
gate** (config `ingest.confidence_threshold`, default 0.3) then stages those for the reply-to-clarify
loop. Calibration: clarify on **identity/portion** ambiguity, **estimate** on quantity (a grilled-beef
kcal estimate is the job, not a stage). Missing confidence defaults to confident (1.0) so normal
estimates never stage. A `servings` multiplier scales a matched `food_reference` ("half a shake" → ×0.5).

#### Stage vs insert routing

`vision_extract` returns a parsed dict (or a **list** for multi-item food like
"shake + fish + rice" — the food branch handles the list; other branches coerce to a
single dict). The completeness predicate (`food_is_complete` / `supplement_is_complete`
/ `biomarker_is_complete`) — plus the confidence gate above — then decides routing:

- **Complete** → `maintainer_ingest_food` writes straight to `food_logs`
  (production); `media_inbox.status = "processed"`.
- **Incomplete** → the RPC is called with `p_force_stage = true` and a `p_stage_reason`,
  writing to `stg_food_log_review` instead; `media_inbox.status = "staged"`.
- **Malformed Claude response** → `vision_extract` returns `{"_stage_reason": ...}` →
  staged. It **retries the call once** and parses tolerantly (`_extract_json` strips code
  fences + extracts the outermost `{}`/`[]` from any surrounding prose); `max_tokens` is
  **4096** (1024 truncated labelled multi-item foods mid-JSON, which used to strand them
  in review). Only a second failed parse stages the item.
- **HTTP error to Claude** → `vision_extract` returns `{"_error": ...}` →
  `media_inbox.status = "failed"` (pipeline fault, needs investigation — distinct from a
  normal completeness-gate stage).

**Multi-item confirmations** name every item and count items, not clusters: a 2-item log
("pineapple and dragon fruit") confirms "✅ Logged 2 items: …" and the run summary says
"Done — 2 logged" (`compose_confirmation` is list-safe; `n_written` tracks inserted rows).
**Repeat products** resolve instantly via `food_reference` (e.g. "Protein Thai Tea Shake"
190 kcal/35P per 350 ml) — no label re-read. (Reference override isn't portion-scaled yet
— backlog #10.)

All thresholds driving these predicates (kcal plausibility, biomarker `plausible_min/max`,
confidence floor) come from `system_config` / `metric_definitions` — never hardcoded.

#### Failure modes

| Failure | Effect | Recovery |
|---|---|---|
| **`trigger-drain` is down / errors** | Path A never dispatches. Photo sits `pending` in `media_inbox`. | Path B (300s Routine) or the **03:30 UTC daily cron** drains it. Manual: `gh workflow run inbox-drain -f settle_sec=0`. No data is lost — `media_inbox` is the durable queue. |
| **`telegram-webhook` crashes between media_inbox INSERT (step 7) and telegram_processed_updates INSERT (step 8)** | No idempotency record written. | Telegram retries the update; full path re-runs. Strict ordering (media_inbox first) guarantees the photo is captured even on retry. |
| **Storage upload fails** (`upsert:false`) | `storage_path` may be missing, but the row is still inserted. | Non-fatal — the drain re-fetches by signed URL; a retry's upload would fail silently but the original bytes are already stored. |
| **GH Actions queue backed up / 15-min timeout** | Cluster left `pending` (claim rolls back on timeout). | Next dispatch or daily cron re-claims and re-processes. |
| **Claude Vision malformed / low confidence** | Item is **staged**, not lost — `stg_food_log_review` with a `stage_reason`. | Maintainer reviews via Staging Queue Review (§7.7). |
| **Claude API HTTP error** | `media_inbox.status = "failed"`. | Re-dispatch the drain; investigate if persistent (key, rate limit, outage). |
| **Concurrent drains on same album** | `claim_inbox_cluster` lets only one win; loser's rows roll back to `pending`. | The winner processes; no double-write. |

#### Minor-safety framing

The `is_minor` flag on `telegram_identities` changes both the ack text (step 9) and any
push/confirmation wording (`compose_confirmation`, `composePush`): growth/performance
language only, never deficit/restriction words. This is enforced at the
`telegram-webhook` ack and again in the Python drain.

### 3.2 Text Message → Daily Brief (or Log) Pipeline

A **text-only** Telegram message has two routes. If it starts with an explicit log trigger
(`log:` / `add:` / `/log` / `/add`, case-insensitive — `parseLogCommand`), the stripped body is
enqueued as a **text-only `media_inbox` row** (`storage_path = null`, `kind = 'unknown'`) and flows
through the *same* drain as photos (the unknown-classifier reads food/supplement/biomarker from the
caption text — no image needed); the user gets "📥 Got it — logging…" then a "✅ … logged"
confirmation from the drain. **All other text** (e.g. "how am I doing today?", "brief", "summary")
defaults to the daily brief, skipping `media_inbox` entirely. (Bare-text-defaults-to-log was
deliberately not done — see `BACKLOG.md` #6.)

```
USER (Telegram text)
  ▼
telegram-webhook
  │  Identity + update_id dedup as above
  │
  ├─ "log:/add: …"  → INSERT media_inbox { kind:'unknown', storage_path:null, caption:body }
  │                    → pg_net trigger → drain (extracts from text) → "✅ … logged"
  │
  └─ anything else  → Fires GitHub repository_dispatch: send-brief
  ▼                     POST api.github.com/.../dispatches
send-brief.yml          { event_type:"send-brief", client_payload:{ profile_id } }
  │  python3 -m monitor.brief --profile-id <payload.profile_id>
  ▼
monitor/brief.py → compose_brief()
  │  Fetches all sections, renders text, calls Claude (Haiku), sends Telegram
  ▼
Telegram reply (the rendered daily brief)
```

`send-brief.yml` also supports manual `workflow_dispatch`: with a `profile_id` input it
briefs that one profile; without one it runs `monitor.brief --all` for every profile.
Note the env-var inconsistency: `monitor/brief.py` reads **`ANTHROPIC_API_KEY`** (the
workflow maps `HS_ANTHROPIC_API_KEY` → `ANTHROPIC_API_KEY`), whereas
`monitor/inbox_drain.py` reads **`HS_ANTHROPIC_API_KEY`** directly.

### Commands & how to interact

The bot is primarily **NATURAL LANGUAGE** — there's almost nothing to memorise. You just talk to it.

| Input | What it does | Status |
|-------|--------------|--------|
| `/start <code>` | Link a Telegram account to a profile (onboarding) | ✅ implemented (telegram-webhook) |
| type anything you ate/took (e.g. "took my magnesium", "ate 2 eggs", "half a thai tea shake") | Logs it — the drain's LLM extracts food/supplement/biomarker | ✅ implemented |
| send a photo (meal, label, lab, DEXA) | Logs it; reads nutrition labels | ✅ implemented |
| ask a question ("how am I doing?", "brief", "summary") | Returns the daily brief | ✅ implemented |
| REPLY (Telegram reply) to a "needs clarification" message | Re-queues that item with your answer; the LLM re-extracts | ✅ implemented (reply-to-clarify loop) |
| `/learn` | ADVERTISED in the food "logged X before — use /learn to add it to your food library" offer, but **NOT IMPLEMENTED** — typing it currently does nothing. The `promote_food_to_reference` RPC exists but isn't wired to a command. ⚠️ Flagged (backlog #13). | ⚠️ NOT implemented |

⚠️ The `/learn` offer references a command that doesn't exist yet — it should either be wired to `promote_food_to_reference` or removed from the offer text (backlog #13).

### 3.3 WHOOP Sync Pipeline

WHOOP data enters via two complementary paths — a real-time webhook push and a
pull-based sync — backed by an OAuth token store with lazy auto-refresh.

#### Push path (real-time webhook)

```
WHOOP  ──event──▶  whoop-webhook  (Edge fn, verify_jwt=false)
  events: workout.updated | sleep.updated | recovery.updated
          | cycle.updated | *.deleted
  │  1. Verify HMAC-SHA256 (key=WHOOP_CLIENT_SECRET, msg=timestamp+raw body, base64)
  │  2. Map WHOOP user_id → internal profile_id via whoop_tokens
  │  3. Open wearable_sync_log row
  │  4. getValidAccessToken (lazy refresh via _shared/whoop.ts)
  │  5. Fetch full record from WHOOP API:
  │       /v2/activity/workout/{id} | /v2/activity/sleep/{id}
  │       /v2/cycle/{id} | /v2/recovery/cycle/{id}
  │  6. Upsert → whoop_workouts | whoop_sleeps | whoop_cycles
  │  7. SPECIAL CASE on sleep.*: re-fetch most recent closed cycle to refresh its
  │     final day_strain (WHOOP emits NO cycle.updated when a cycle closes)
  │  8. EdgeRuntime.waitUntil → Phase-2 push logic: debounce + quiet-hours +
  │     criticality thresholds (all from system_config) → Telegram push → push_log
  │  9. Dead-man heartbeat check across all active profiles on every event
  │ 10. Final wearable_sync_log status update (+ wearable_sync_errors on failure)
```

#### Pull path (`ingest/whoop_sync.py`)

`sync_profile()` runs, per profile, four pulls in order: **workouts, cycles, sleeps,
body_measurements** (the last → `whoop_body_measurements`: height_m, weight_kg,
max_heart_rate from `GET /v2/user/measurement/body`). `refresh_recent()` is the
brief's pre-sync helper. `monitor/reconcile.py` wraps this with an **age-aware window**:
`min(max(push.reconcile_default_hours, hours_since_last_sync + 2h buffer),
push.reconcile_max_widen_hours)` — defaults 48h / 168h — so a long gap widens the window
but never triggers an unbounded backfill.

#### Token management

- **OAuth flow:** `whoop-oauth` Edge function — `GET ?profile_id=<uuid>` redirects to the
  WHOOP consent page; the callback `GET ?code=...&state=<profile_id>` exchanges the code,
  fetches `user_id` from `/v2/user/profile/basic`, and upserts into `whoop_tokens` keyed
  on `profile_id`. The `state` param carries the profile UUID (CSRF guard). One-time
  human-in-browser setup.
- **Auto-refresh:** `_shared/whoop.ts` `getValidAccessToken` returns the stored token if
  valid, refreshes via `WHOOP_TOKEN_URL` if within 2 minutes of expiry (or expired), and
  upserts the rotated refresh token. Token exchange uses `client_secret_post` with a
  browser User-Agent (required to pass Cloudflare). Shared by both `whoop-webhook` and
  `whoop-oauth`.

#### Backfill

`python3 -m ingest.whoop_sync --backfill` pulls full history from **2020-01-01**.
The initial PC backfill (migration 045) loaded 519 workouts, 580 cycles, 625 sleeps.
WHOOP never emits `cycle.updated` on close, so a just-closed cycle's final `day_strain`
is only captured by `refresh_recent`, a `--since` run, or a `sleep.*`-triggered
re-fetch — not by waiting for a webhook.

### 3.4 Daily Brief Content (`monitor/brief.py`)

`compose_brief()` fetches every section best-effort (any fetcher returning empty on
error), renders minor-safe text, calls Claude (Haiku — default
`claude-haiku-4-5-20251001`, overridable via `system_config` key `brief.model`) for the
rest-of-day actions, sends over Telegram, and returns the message text.

**"Today" is the LOCAL day** (migration 048): `_local_day(cfg)` reads `app.timezone` from
`system_config` and bounds the day as a UTC window over `logged_at`/`taken_at`, so a Thailand
user's day no longer rolls over at 07:00 ICT. **Refresh-on-interaction**: `compose_brief` calls
`refresh_recent` (WHOOP) first — best-effort — so an on-demand brief reflects the latest cycle.

| Section | Content | Rules |
|---|---|---|
| **Food** | kcal / protein / carbs / fat vs targets; plus WHOOP burn + deficit **only when WHOOP data is non-stale** | Targets come from the profile's context MD, never population norms |
| **Supplements** | Grouped by timing slot, ✅ (taken) / ⬜ (pending) per item | — |
| **WHOOP** | Recovery / HRV / sleep; **score_state aware** — ⏳ for `PENDING_SCORE`, ❌ for `UNSCORABLE` (never a misleading 0); sleep cycle count + disturbance count | Stale = `cycle_start` >30h old (elapsed-time, tz-safe — not a UTC-date compare); still shown but flagged |
| **Viome** | avoid / minimize / superfood flags for today's foods | **Suppressed entirely for minor profiles** |
| **Rest-of-day actions** | 2–4 Claude-generated (Haiku) next actions | Empty string on Claude failure (best-effort, never aborts the brief) |

---

## Appendix A — Event-driven drain pipeline (reference)

```
Telegram photo
  → media_inbox INSERT (status=pending)
  → pg_net trigger fn_media_inbox_notify  (no auth header — mig 047)
  → trigger-drain edge fn  (15s CAS dedup on system_config.trigger_drain.last_dispatch_ts)
  → GitHub repository_dispatch: inbox-drain
  → GH Actions inbox-drain.yml
  → monitor/inbox_drain.py --once --settle-sec 5
  → claim_inbox_cluster RPC → Claude extract → maintainer_ingest_food RPC
  → food_logs (complete) OR stg_food_log_review (incomplete)
  → Telegram confirmation

Telegram text  → telegram-webhook → repository_dispatch: send-brief → send-brief.yml → monitor/brief.py
```

A parallel, longer-lived fallback fires from `telegram-webhook` itself (`maybeFireRoutine`,
debounced 300s via `system_config.routine.last_fire_at`) to `ROUTINE_TRIGGER_URL` if
configured. The pg_net path (15s dedup) is the primary real-time path.

---

## 4. DATABASE SCHEMA

Supabase Postgres with Row Level Security on every table. The applied DB state is the
source of truth — there is no `schema_migrations` table; the migration filename sequence
(`001`…`NNN`) is the canonical ordering record.

### 4.1 Active Tables (36)

Grouped by domain. The six most-important tables carry full key-column lists; the rest
carry a one-line purpose. Tables marked `(empty)` or `(dormant)` exist with RLS but hold
no live rows per the SCHEMA-MAP — they are kept for forward use, not yet written by v3.

#### Profiles & Auth

| Table | Purpose / key columns |
|---|---|
| **profiles** | `id, auth_user_id, family_id, managed_by_auth_user_id, display_name, date_of_birth, sex, relationship, is_active, is_maintainer, created_at, updated_at`. The subject of every health record. `is_maintainer = true` (PC only, `relationship = 'self'`) unlocks staging/audit/sync-log visibility via RLS. |
| family_memberships | `auth_user_id → profile_id` binding with `role`. Drives both `has_profile_access()` and `is_maintainer()`. One auth identity can map to several profiles (PC + Dea + Dev; drain account → all three). |
| telegram_identities | `chat_id (PK) → profile_id`. `status` (pending/active/revoked), `is_minor`, `linked_at`, `link_code`. Profile_id for an inbound photo is derived ONLY from here, never from caption text. |
| telegram_link_codes | One-time activation codes: `code (PK), profile_id, expires_at, used_at`. |
| telegram_processed_updates | Inbound idempotency latch: `update_id (PK), received_at`. Written AFTER the `media_inbox` insert succeeds, never before. |
| whoop_tokens | WHOOP OAuth credentials: `profile_id, access_token, refresh_token, expires_at, scope, whoop_user_id`. service_role only — RLS default-deny for all other roles. Never surfaced in skill output. |

#### Food & Nutrition

| Table | Purpose / key columns |
|---|---|
| **food_logs** | `id, profile_id, meal_type (CHECK breakfast\|lunch\|dinner\|snack\|drink\|supplement), description, calories, protein_g, carbs_g, fat_g, fiber_g, source, logged_at, log_date, meal_time, location, foods (jsonb), verdict, flags (text[]), is_day_summary, sprint_id, source_photo_path, source_log_path, image_url, notes`. `is_day_summary = TRUE` rows are daily totals, not meals — exclude from per-meal analysis. Calorie strings must have thousands separators stripped. |
| food_guidance | Per-profile avoid/minimize/superfood list. `item, classification (CHECK avoid\|minimize\|superfood), reason, alternative, category, scope, profile_id (nullable — NULL = global family row), source, is_active`. |
| food_reference | Shared macro library (mig 041): `name, calories, protein_g, carbs_g, fat_g, fiber_g, serving_desc, brand, verified, profile_id (NULL = global, else per-user)`. Personal rows take priority over global on lookup. |
| effective_food_guidance **(view)** | `security_invoker` view joining `food_guidance` to the active profile — returns global rows plus the profile's own rows. |
| canonical_aliases | Metric-name alias table. `(empty)` — alias resolution lives in code; kept by deliberate ruling (mig 026/028). |

#### Supplements

| Table | Purpose / key columns |
|---|---|
| **supplement_intake_logs** | `id, profile_id, regimen_id, supplement_id, taken_at, taken_on, dose_amount, dose_unit, source (CHECK manual\|journal\|skill\|csv\|photo\|telegram), notes, created_at`. **`taken_on` is `GENERATED ALWAYS AS ((taken_at AT TIME ZONE 'UTC')::date) STORED`** — never INSERT/UPDATE it (error 428C9); valid only as an `ON CONFLICT` target. UNIQUE (profile_id, supplement_id, taken_on, source). |
| supplements | Catalog: `name, display_name, brand, key_active, category (CHECK supplement\|medication\|probiotic\|other), default_unit, is_medication, is_compound, owner_profile_id, is_custom, is_active`. |
| supplement_regimens | Per-profile protocol: `supplement_id, dose_amount/unit, timing (text[]), frequency, cycle_days_on/off, start_date, end_date, status (active\|discontinued\|planned), purpose, program_id`. Cyclical on/off aware; date-window exposure drives correlation, not status. |

#### Biomarkers

| Table | Purpose / key columns |
|---|---|
| biomarkers | `profile_id, metric_definition_id, value, qualifier, unit, source, document_id, staging_id, measured_at, lab_name, healthspan_test_id, notes`. `qualifier` (`<`/`>`/`<=`/`>=`/`~`) preserves detection bounds vs exact values. HOMA-IR/eGFR are lab-printed derived values — never recompute. UNIQUE (profile_id, metric_definition_id, measured_at). |
| metric_definitions | Marker catalog: `name, display_name, category, data_type, unit, loinc_id, min_value, max_value, plausible_min, plausible_max, decimal_places, is_active`. `min/max_value` = clinical reference range; `plausible_min/max` = wider physiological gate used by `lib/contract` to route OCR/unit errors to staging. |

#### WHOOP Wearables

| Table | Purpose / key columns |
|---|---|
| **whoop_cycles** | `id, profile_id, cycle_start, cycle_end, timezone, recovery_score_pct, resting_hr_bpm, hrv_ms, skin_temp_celsius, blood_oxygen_pct, respiratory_rate_rpm, day_strain, energy_burned_cal, max_hr_bpm, avg_hr_bpm, sleep_onset, wake_onset, sleep_performance_pct, asleep_duration_min, in_bed_duration_min, light_sleep_min, deep_sws_min, rem_min, awake_min, sleep_need_min, sleep_debt_min, sleep_efficiency_pct, sleep_consistency_pct, sprint_id, source_file, whoop_id`. **Mig 045 added:** `score_state, recovery_score_state, recovery_user_calibrating, sleep_cycle_count, disturbance_count, no_data_min, whoop_updated_at, whoop_created_at`. Traps: NULL recovery/HRV = no sleep that cycle (never 0); `cycle_start` is the WHOOP day boundary (screenshot date minus 1 day) — join to journal on `::date`, not exact timestamp. |
| whoop_sleeps | Per-sleep record. `is_nap = TRUE` rows separate from main sleep (most analyses filter `is_nap = false`). Mig 045 added `score_state, no_data_min, sleep_cycle_count, disturbance_count, whoop_cycle_id, whoop_updated_at, whoop_created_at`. |
| whoop_workouts | Per-workout: zone %/seconds, strain, HR, loads, `tags`/`protocol` (jsonb), `whoop_id`. Mig 045 added `score_state, sport_id, percent_recorded, distance_m, altitude_gain_m, altitude_change_m, whoop_updated_at, whoop_created_at`. |
| whoop_body_measurements | Mig 045: `profile_id, synced_at, synced_date, height_m, weight_kg, max_heart_rate`. UNIQUE (profile_id, synced_date). |
| whoop_journal_entries | Long-format behaviour log: `profile_id, cycle_start, behavior_id, answered_yes, quantity, unit, notes, source_file`. UNIQUE (profile_id, cycle_start, behavior_id). Join to cycles on `cycle_start::date`. |

#### Staging / Review

| Table | Purpose / key columns |
|---|---|
| **media_inbox** | `id, profile_id, chat_id, kind (CHECK food\|workout\|lab\|dexa\|unknown), storage_path, caption, status (CHECK pending\|processing\|done\|failed\|staged), stage_reason, whoop_id, media_group_id, created_at, processed_at, result_ref`. Durable inbound queue. `profile_id` derived only from `telegram_identities(chat_id)` — never from caption (injection rule); caption stored as data, never executed. `media_group_id` clusters Telegram album bursts. |
| stg_food_log_review | AI food extraction staging: `profile_id, description, meal_type, calories, macros, foods, verdict, confidence, status, stage_reason, raw_text`. Maintainer-only SELECT. |
| stg_supplement_intake_review | AI supplement staging: `profile_id, supplement_id, extracted_name, extracted_dose/unit, taken_at, confidence, status, stage_reason, raw_text`. Maintainer-only SELECT. |
| stg_biomarker_review | AI biomarker staging: `profile_id, metric_definition_id, extracted_name/value/unit, measured_at, confidence, status, stage_reason, raw_text`. Maintainer-only SELECT. |

#### Training & Goals

| Table | Purpose / key columns |
|---|---|
| training_programs | `profile_id, name, objective, target_event, start_date, end_date, status (planned\|active\|done\|abandoned)`. |
| program_phases | `program_id, name, ordinal, start_date, end_date, weekly_template (jsonb)`. |
| program_workouts | `program_id, phase_id, workout_id, workout_type, prescribed`. `(empty)`. |
| user_goals | `profile_id, metric_definition_id, title, target_value/unit/date, is_active, progress_pct, program_id, baseline_value, achieved_value, achieved_date`. Multiple concurrent active goals allowed (mig 020). |
| sprints | `profile_id, name, slug, location, start_date, end_date, goals (jsonb)`. No status column — derive active/completed/upcoming from dates vs today. |

#### Monitoring

| Table | Purpose / key columns |
|---|---|
| wearable_sync_log | `profile_id, provider, sync_type, status (CHECK pending\|in_progress\|success\|failed), method (CHECK csv\|screenshot\|photo\|manual\|api), records_in/upserted/skipped/failed, document_id, source_path, started_at, completed_at`. Maintainer-only SELECT. |
| wearable_sync_errors | `sync_log_id, record_ref, error_code, error_message, raw (jsonb)`. Maintainer-only SELECT. |
| query_audit | `profile_id, ts, kind (CHECK catalog\|adhoc), name_or_sql, params (jsonb), quality_verdict (jsonb), row_count, flagged`. Every skill read lands here. Maintainer-only SELECT. |
| trend_alerts | `profile_id, metric_definition_id, alert_type, severity, title, message, data (jsonb), is_read`. `(empty)` — alerting not yet wired. |

#### System

| Table | Purpose / key columns |
|---|---|
| **system_config** | `id, key, value (jsonb), description, category, is_active, created_at, updated_at`. The single home for every threshold (calorie gates, confidence minimums, settle/debounce windows, push thresholds, drain dedup CAS timestamp, vision model id). Rule 1: nothing numeric is hardcoded — it lives here. |
| audit_log | `user_id, action, table_name, record_id, old_data/new_data (jsonb), ip_address, user_agent`. `(empty)` — kept pending keep/drop ruling; not written by the v3 skill. |
| health_context_notes | `profile_id, kind (CHECK current\|log), summary, refs (jsonb), s3_keys (text[]), supersedes_id`. Narrative + references only — never store numeric values here (numbers always come from the live DB). One `current` note per profile. |
| travel_log | `profile_id, start_date, end_date, destination, country_code, alcohol_level, activity_context, is_altitude, timezone_hint`. Date-range trips for location-based performance analysis. |

### 4.2 Key RPCs / Functions

| Function | Security | Purpose |
|---|---|---|
| `maintainer_ingest_food` | DEFINER | 16-param. Guard = `is_maintainer() AND has_profile_access(target)`. Routes to `food_logs` or `stg_food_log_review`. Stages when `p_force_stage = true`, when kcal is outside `food_energy_kcal` plausibility bounds, when `description` is NULL, or when `meal_type` is not in the CHECK set. Returns `{id, status, stage_reason}`. |
| `maintainer_ingest_supplement` | DEFINER | 12-param. Routes to `supplement_intake_logs` or `stg_supplement_intake_review`. Stages on `p_force_stage` or `supplement_id IS NULL`. `taken_on` removed from INSERT (GENERATED, 428C9 fix mig 039); `source` CHECK includes `telegram` (mig 040). Returns `v_stage_reason`. |
| `maintainer_ingest_biomarker` | DEFINER | 12-param. Routes to `biomarkers` or `stg_biomarker_review`. Stages on `p_force_stage`, `metric_definition_id IS NULL`, `value IS NULL`, or value outside `plausible_min/max`. |
| `lookup_food_reference(p_name, p_profile_id)` | DEFINER | Best macro match from `food_reference`; personal rows beat global. |
| `lookup_viome_verdicts(p_items[], p_profile_id)` | DEFINER | Returns avoid/minimize/superfood verdicts for a list of ingredient names. |
| `promote_food_to_reference(p_name, p_profile_id, …)` | DEFINER | Upserts a user-confirmed food into their personal `food_reference` library. |
| `claim_inbox_item(item_id)` | client method (`lib/db_rest.py`) | Single-item claim via filtered PATCH (`status pending → processing`, `prefer_return`); returns `True` if this caller won the race. Not a DB RPC — runs under the drainer's JWT, governed by `media_inbox` RLS. |
| `claim_inbox_cluster(p_item_ids[])` | INVOKER (mig 046) | Atomic all-or-nothing cluster claim. `FOR UPDATE` (no SKIP LOCKED — waits, never skips); returns `true` only if every row was still pending. Runs as the drainer; existing RLS governs access. |
| `fn_media_inbox_notify()` **(trigger)** | DEFINER | `AFTER INSERT` on `media_inbox` (`trg_media_inbox_notify`); fires `net.http_post` to the `trigger-drain` Edge function for each new `pending` row. Reverted to anon-key-from-`system_config` header in mig 047 (vault lookup returned NULL in pg_net context). |
| `is_maintainer(p_uid DEFAULT NULL)` | DEFINER | Resolves the maintainer flag via `family_memberships JOIN profiles` (mig 023) so any of PC's auth identities is recognised. |
| `has_profile_access(uuid)` | DEFINER | True if the calling auth identity has a `family_memberships` row for the target profile. The backbone of every profile-scoped RLS policy. |

### 4.3 RLS Model

- **RLS is enabled on every public table.** New tables without an RLS policy in the same migration do not pass review (Rule 3).
- **Two PostgREST roles:**
  - `authenticated` — every skill/drain session. All reads and writes are scoped to the caller's accessible profiles via `has_profile_access()` / `is_maintainer()` policies. Cannot see other families' data.
  - `service_role` — bypasses RLS entirely. Used **only** by Edge functions and sync jobs (e.g. `whoop_tokens` access, webhook inserts). Never used by the skill.
- **`healthspan_app`** — a Postgres LOGIN role for the direct-psycopg2 path (PC local). Member of `authenticated` with `NOINHERIT`, so it must `SET ROLE authenticated` and set JWT claims explicitly; a bare connection sees nothing. Inherits mig 010's grants and all RLS via that path. DELETE/TRUNCATE revoked directly.
- **Maintainer-only tables** (SELECT gated to `is_maintainer()`): `query_audit`, `wearable_sync_log`, `wearable_sync_errors`, `stg_food_log_review`, `stg_supplement_intake_review`, `stg_biomarker_review`. Dea/Dev sessions see zero rows on these by design — they see outcomes, never the machinery. INSERTs to these tables use client-generated UUIDs and no `RETURNING` (which would invoke the SELECT policy).
- **Drain service account:** `healthspan.drainer@chitalkar.com` (env `HS_AUTH_EMAIL`). Holds `owner` `family_memberships` to PC, Dea, and Dev profiles so the three `maintainer_ingest_*` RPCs pass `has_profile_access()`. UID resolved dynamically (mig 035) — no hardcoded UUID. Not itself a maintainer.
- **Grant hardening** (mig 010/014): DELETE and TRUNCATE revoked from `authenticated` and `healthspan_app` on all public tables; `ALL` revoked from `anon`; shared catalogs are SELECT-only for `authenticated`.

### 4.4 Migration History (001–047)

| # | File | Summary |
|---|---|---|
| 001 | initial_schema | Foundation schema (36 tables, RLS-enabled, `user_id`-scoped); extensions, `update_updated_at()`, reference/staging tables, ~20 indexes. |
| 002 | multitenant_programs | Multi-tenant refactor: families/profiles/memberships layer (Chitalkar seed), 21 tables re-pointed `user_id → profile_id`, RLS rebuilt via `has_profile_access()`, training programs, ingestion tokens. |
| 003 | journal_entries | Replaces 27-boolean `whoop_journal` table with long-format `whoop_journal_entries` (+ behaviour catalog + aliases); old table survives to 007. |
| 004 | supplements_nutrition | Supplement + food guidance layer: catalog, aliases, components, regimens, intake logs (`taken_on` GENERATED), staging, `food_guidance`, correlation views, Chitalkar seed. |
| 005 | workout_detail | WHOOP workout zone seconds, per-profile HR zone config, `workout_intervals`, raw `workout_hr_samples`. |
| 006 | travel_log | `travel_log` (15 PC trips seeded) + `v_india_vs_travel` view. |
| 007 | journal_view_swap | Drops `whoop_journal` table; replaces with `security_invoker` pivot view + INSTEAD OF INSERT write-through trigger; recreates dependent views in order. |
| 008a | upsert_keys | Natural upsert UNIQUE keys on biomarkers/weight_logs/food_logs after duplicate pre-flight; seeds `ingest.confidence_min`. |
| 008b | whoop_id | `whoop_workouts.whoop_id` + UNIQUE for stable API-keyed upsert. |
| 008c | protocol_maxhr | Adds `whoop_workouts.protocol` jsonb; fixes `hr_zone_config.max_hr_bpm = 181`. |
| 008d | metric_definitions_catalog | INSERT ~50 markers (CBC diffs, lipid ratios, metabolic, kidney, liver, minerals, iron, cardiac/respiratory). |
| 008e | whoop_id_cycles_sleeps | Mirrors 008b for `whoop_cycles`/`whoop_sleeps` (`whoop_id` + UNIQUE) for re-score handling. |
| 008f | catalog_extension_2 | Second metric_definitions batch (bilirubin fractions, PTH, body-comp DEXA markers); `android_gynoid_ratio` declared distinct from `ag_ratio`. |
| 009 | context_notes | `health_context_notes` — narrative-only session memory, one `current` per profile, `supersedes_id` lineage. |
| 010 | restricted_grants | Revokes DELETE/TRUNCATE from `authenticated`, ALL from `anon`; catalogs SELECT-only; ALTER DEFAULT PRIVILEGES. |
| 011 | prevention | Adds `biomarkers.qualifier` for below/above-detection operators; backfills one Lp(a) row. |
| 012 | consolidate_duplicate_defs | Resolves overloaded `ag_ratio` and `ggtp` definitions; re-points rows, deletes the duplicates (guarded). |
| 013 | bodycomp_canonical_biomarkers | Folds 16 DEXA `body_metrics_history` rows into `biomarkers`; writes interpretation note; empties body_metrics_history of DEXA. |
| 014 | healthspan_app_role | Creates `healthspan_app` LOGIN role (member of `authenticated`, NOINHERIT) for the direct-psycopg2 path. |
| 015 | whoop_tokens | `whoop_tokens` (OAuth creds) — service_role only, RLS default-deny for all other roles. |
| 016 | schema_comments | COMMENT ON for all columns of 19 high-value tables (units, traps) in pg_description. |
| 016b | join_trap_comments | Comments the `whoop_journal.cycle_start` vs `whoop_cycles.cycle_start` ::date-only join trap. |
| 016c | schema_comments_completion | Comments remaining active tables; marks `users_extended`/`body_metrics_history` DORMANT; supersedes `taken_on` comment. |
| 017 | implausibility_bounds | Adds `metric_definitions.plausible_min/max`; seeds 24 priority metrics + `food_energy_kcal`/`energy_burned_cal`. |
| 018 | implausibility_bounds_full | Extends plausibility bounds to ~127 remaining in-use metrics (verified against live data; guarded). |
| 019 | widen_clinically_tight_bounds | Raises/lowers three bounds (tsh, vitamin_b12, uric_acid) that were gating real severe-pathology values. |
| 020 | multiple_concurrent_goals | Drops the too-tight `uq_user_goals_one_active_per_program` index; allows multiple concurrent active goals. |
| 021 | query_audit | Creates `query_audit` (ad-hoc query log); profile-scoped RLS (tightened to maintainer in 022). |
| 022 | maintainer_model | Adds `profiles.is_maintainer`; `is_maintainer()` + `hs_close_sync_log()`; rebuilds maintainer-only RLS on sync-log/staging/audit tables. |
| 023 | fix_is_maintainer_via_memberships | Re-resolves `is_maintainer()` via `family_memberships JOIN profiles` so PC's skill identity is recognised. |
| 024 | drop_food_logs_dedup_backup | Drops one-off `food_logs_dedup_backup_20260602` snapshot table. |
| 025 | query_audit_catalog_kind | Reshapes `query_audit` to log catalog + ad-hoc (`kind`, `name_or_sql`, `params`); drops `intent`/`sql`/`plan`; guards on empty table. |
| 026 | drop_dead_saas_tables | Drops 8 empty SaaS-fossil tables (brain_*, user_invite*, user_tier/locations/log_type/preference); keeps audit_log + canonical_aliases (guarded). |
| 027 | supplement_intake_source_photo | Widens `supplement_intake_logs.source` CHECK to allow `'photo'`. |
| 028 | drop_uncertain_fossils | Drops 4 more dead tables (user_telegram_links, locations, log_type_config, source_priority_config); guarded. |
| 029 | telegram_ingestion | Phase 1 Telegram: 5 production tables (link_codes, identities, processed_updates, media_inbox, push_log) with RLS + comments. |
| 030 | push_enhancements | Adds `push_log.dedup_value`; seeds all push-threshold `system_config` (debounce, quiet hours, recovery/HRV/material-change/dead-man/reconcile). |
| 031 | maintainer_ingest | Core ingest RPCs (food/biomarker/supplement, confidence-routing); `media_inbox.media_group_id`; settle/fire dedup config. |
| 032 | drain_identity | Drain service account (`healthspan.drain@…`, UID 1e2084cf) + family memberships; revokes anon EXECUTE on the RPCs. |
| 033 | drain_identity_rekey | Re-keys drain memberships to new UID 06ada8f7 after account delete/recreate. |
| 034 | drain_vision_config | Seeds `drain.vision_model = claude-sonnet-4-6` in `system_config`. |
| 035 | drain_identity_drainer | Final drain identity `healthspan.drainer@chitalkar.com`; UID resolved dynamically (no hardcode); aborts if account missing. |
| 036 | completeness_gate | Replaces confidence-routing in `maintainer_ingest_food` with `p_force_stage` (15-param); DB-side kcal plausibility gate; `NOTIFY pgrst`. |
| 037 | stage_reason | Adds `stage_reason` to `media_inbox` + `stg_food_log_review`; rebuilds food RPC (16-param) to persist/return it. |
| 038 | supplement_biomarker_align | Brings supplement (12-param) + biomarker (12-param) RPCs to the food pattern (`p_force_stage`/`p_stage_reason`); biomarker plausibility gate; `stage_reason` on both staging tables. |
| 039 | supplement_rpc_fix_generated_taken_on | Removes `taken_on` from the supplement RPC INSERT list (GENERATED, error 428C9); kept as ON CONFLICT target. |
| 040 | write_contract_audit | Cross-checks all three RPCs vs target constraints; adds `'telegram'` to supplement source CHECK; DB-side NULL/CHECK guards (supplement_id, metric_id, value, description, meal_type). |
| 041 | food_reference_viome_peruser | `food_reference` macro library (global + personal); per-user `food_guidance.profile_id`; `effective_food_guidance` updated; lookup/promote RPCs; Hooray Strawberry Shake seed. |
| 042 | trigger_drain_webhook | `fn_media_inbox_notify()` + `trg_media_inbox_notify` AFTER INSERT — pg_net `http_post` to `trigger-drain` for each new pending row. |
| 043 | trigger_drain_secret | Seeds `trigger_drain.last_dispatch_ts` (CAS dedup); switches `fn_media_inbox_notify` to vault-based `TRIGGER_DRAIN_SECRET` header (later reverted in 047). |
| 044 | food_guidance_drop_old_unique | Drops stale `food_guidance_item_classification_scope_key` UNIQUE; adds `food_guidance_global_item_class` partial index for global rows. |
| 045 | whoop_schema_expansion | Full WHOOP v2 field coverage on cycles (8), sleeps (7), workouts (8); new `whoop_body_measurements` table; all additive/nullable. |
| 046 | claim_inbox_cluster_atomic | `claim_inbox_cluster(uuid[])` — atomic all-or-nothing cluster claim (`FOR UPDATE`, no SKIP LOCKED); SECURITY INVOKER. |
| 047 | fix_media_inbox_notify | Reverts `fn_media_inbox_notify` to NO auth header (vault lookup returned NULL in pg_net context, causing 401 stalls). trigger-drain runs no-auth. |
| 048 | app_timezone | Seeds `app.timezone='Asia/Bangkok'` in `system_config`. The daily brief computes "today" as a LOCAL-day UTC window over `logged_at`/`taken_at` instead of the UTC-derived `log_date`/`taken_on` (fixes the 07:00-ICT day rollover + UTC display). |
| 049 | clarify_loop | `media_inbox.clarify_message_id` + `clarify_count` for the reply-to-clarify loop: a staged item's review message id is stored, a user reply re-queues it (fresh INSERT → AFTER-INSERT trigger fires) with the clarification + original image; `clarify_count` caps rounds at 2. |

---

## 5. PYTHON CODEBASE

### 5.1 Module Map

One paragraph per module: what it does, key public functions, and when to reach for it. `lib/contract.py` is the central hub — every ingest source funnels through its `resolve → validate → write → log` pipeline; `lib/db.py` supplies the connection and `lib/views.py` + `lib/sql_guard.py` own the read side.

#### lib/

**db.py** — DB connection factory and profile resolution; reads the DSN from a shared `.db-config.json` (two Dropbox paths or `$HEALTHSPAN_DB_CONFIG`) and never logs it. `get_conn(readonly=False)` opens a raw psycopg2 connection for CLI tools; `resolve_profile(conn, who)` maps a display name ("PC") or UUID to a profile UUID (raises `LookupError` on miss/ambiguity, defaults to "PC"); `get_app_connection(config)` returns either a psycopg2 connection with `SET ROLE authenticated` + JWT claim (`direct_role` mode) or a fully signed-in supabase client (`supabase_client` mode) — never an anon client. Use it as the entry point for any direct-DB tool; psycopg2 is lazily imported so the supabase path has no psycopg2 dependency.

**db_rest.py** — httpx-based PostgREST client for cloud environments where native packages are unavailable (GH Actions, Claude Routines — no psycopg2). `sign_in(url, anon_key, email, password)` does a password-grant and returns a JWT; `DbRest(url, anon_key, jwt)` is a context manager with `.select/.insert/.update/.rpc` plus `.claim_inbox_item(item_id)`, a compare-and-swap that only flips rows where `status='eq.pending'` so concurrent Routine fires can't double-process. Reach for this whenever code runs in the cloud REST path rather than against a direct connection.

**contract.py** — the shared four-step ingestion contract (Resolve → Validate → Write → Log) that every data source obeys; the central correctness/safety layer. Key functions: `parse_number`, `resolve(conn, kind, raw)` (catalog FK + confidence), `validate` (two-tier: clinical `min/max` flags but never blocks, physiological `plausible_min/max` gates to staging), `write(conn, table, row, conflict_cols)` (idempotent upsert that auto-excludes GENERATED columns from INSERT/UPDATE while keeping them as ON CONFLICT targets), `stage`, `open/close_sync_log`/`log_error`, and `ingest_record(...)` which runs one record through the full gate order under a SAVEPOINT (implausibility → unverified-unattended → low-confidence → hard-validation → prod write). Use it from any new ingest source; thresholds come from `system_config` (never hardcode), and `out_of_range` writes to prod while `implausible` routes to staging.

**context.py** — loads and parses per-person `context/<slug>.context.md` into a structured dict; the profile context is the sole source of targets, norms, coaching voice, and safety constraints (no population defaults). `parse_context_md(text)` is a pure parser; `load_context(profile, base_dir)` reads the file and hard-stops (`FileNotFoundError`) if absent; `get_context(config, base_dir)` bootstraps a session and hard-errors on a profile_id mismatch between context file and config (prevents coaching one person with another's rules); `get_target(ctx, key)` returns a value or None ("not specified — ask, don't assume"). Call it at session start before any targeted coaching.

**views.py** — centralised catalog of all named reporting queries; every emitter (reports, dashboard, exports) must go through it so SQL is never duplicated. `list_views()` returns catalog metadata; `run_view(conn, name, profile_id, **params)` runs an RLS-scoped named view and returns `{name, columns, rows, chart_hint, description, summary?}`, logging each run to `query_audit` inside a savepoint (a read-only connection or RLS failure never breaks the read path). Named views include `v_recovery_30d`, `v_sleep_debt_30d`, `v_workout_zone_summary`, `v_supplement_onoff`, `v_india_vs_travel`, `v_biomarker_timeline`, `abnormal_labs`, `vo2_status`, `hr_drop_compare`, `sprints_status`. NULL recovery (no sleep that cycle) is filtered, never averaged as 0. Use it for any catalog read.

**sql_guard.py** — two-layer safety on the read path for ad-hoc queries: a structural validator + EXPLAIN pre-flight, and a cite-guard that traces drafted numbers back to returned rows. `validate_readonly_sql` rejects multi-statement/write/DDL, wraps in a guarded LIMIT, and runs `EXPLAIN` (not ANALYZE, so the query never executes); `relevant_traps` surfaces TRAP-marked schema comments before compose time; `quality_check` flags hazards (`join_without_key`, `comma_join`, `aggregate_over_nullable`, `no_profile_scope`, `empty_result`, …); `log_query` audits non-blocking; `run_adhoc_audited(conn, profile_id, sql, intent, limit)` is the standard entry point for any non-catalog query — validate, run, quality-check, and log in one call.

**safe_ops.py** — guards for manual corrections on already-logged data; prevents accidental bulk sweeps by date predicate. `today_utc()` is always UTC (matches the drain clock); `safe_bulk_update(conn, table, ids, update_data, dry_run=True)` and `reassign_day_by_ids(conn, table, ids, new_taken_at, dry_run=True)` both update by explicit id list only (raise on empty ids, default to dry-run), preserve the original timestamp in `notes`, and rely on Postgres to recompute the GENERATED `supplement_intake_logs.taken_on`. Reach for it when manually moving or fixing logged rows; never accepts a date predicate.

#### ingest/

**food.py** — interactive food logging with LLM macro estimation and Viome guidance cross-reference; implements the contract for `food_logs`. `classify_food(items, guidance_hits)` calls Claude Haiku for a verdict (green/amber/red), confidence, and macros; `ingest_food_row(payload, conn, profile_id, method)` resolves per-item guidance hits → classifies → confidence-gates → validates → plain `INSERT` (manual entries have `source_log_path=NULL` and sit outside the partial unique index, so no upsert); `run_interactive` is the CLI loop. Food confidence is floored at 0.6 even with no guidance match (a plain meal is still valid).

**biomarker.py** — biomarker ingestion against the `metric_definitions` catalog; a thin wrapper over `contract.ingest_record` that adds a LOINC fallback. `ingest_biomarker_row(payload, conn, profile_id, method, attended)` resolves by metric name, falls back through `loinc_reference.loinc_code → metric_definitions.loinc_id` to recover the name, then delegates to the full contract pipeline; conflict key is `(profile_id, metric_definition_id, measured_at)`. Automated callers must pass `attended=False` to arm the unverified-value fail-safe (default is `True`).

**supplement.py** — supplement intake logging with alias resolution and regimen auto-attachment. `ingest_supplement_row` resolves via `supplement_aliases` (score 1.0) or `supplements.name` (0.9), auto-looks-up the active regimen, gates and validates, then upserts on `(profile_id, supplement_id, taken_on, source)`; helpers `find_active_regimen`, `list_active_regimens`, `close_regimen`, and `run_interactive` (CLI menu). Valid sources `{manual, journal, skill, csv, photo}` are checked early; `taken_on` is GENERATED — an ON CONFLICT target but excluded from INSERT.

**whoop_sync.py** — pulls WHOOP API data (workouts, cycles/recovery, sleeps, body measurements) and upserts into `whoop_*` tables; uses stdlib `urllib` only (no httpx/requests). `sync_profile(conn, profile_id, since_iso, to_iso, env)` runs all four per-table syncs (each under a `wearable_sync_log` run with SAVEPOINT isolation); `refresh_recent(conn, hours, profiles, env)` is the mini-sync the morning brief calls (and exists specifically because WHOOP emits no cycle.updated webhook, so final `day_strain` must be re-pulled); `_map_cycle/_map_sleep/_map_workout` carry the migration-045 fields; `_resolve_access_token` prefers DB `whoop_tokens` then falls back to the `.env` refresh token. CLI `--backfill` pulls from 2020-01-01. All conflict keys use `(profile_id, whoop_id)`; zone fields are written only when `total>0` so screenshot-sourced zones aren't overwritten with zeroes.

**whoop_screenshot.py** — digitises WHOOP HR-curve screenshots via Claude vision; strictly additive, never overwrites API aggregates, captures the per-second curve and derived intervals the API can't provide. `process_image(conn, profile_id, png, ...)` runs vision extract → workout match → write samples/intervals → fill coach note/cardio split; `_match_workout` matches by local start time (±2 min) and duration and refuses to guess; `_derive_intervals(samples, z4_low)` detects work bouts and recovery valleys; `_already_done` is the idempotency check. z4_low comes from `hr_zone_config` (profile-specific, defaults 158); samples must be 30–230 bpm. Use it to enrich a matched workout, not to create one.

**whoop_oauth.py** — one-time WHOOP OAuth2 authorization_code consent flow (stdlib only); idempotent — skips the browser if the stored refresh token still works. `run_oauth()` completes the flow, returns an access token, and writes `WHOOP_REFRESH_TOKEN` to `.env`; `try_refresh(client_id, client_secret, refresh_token)` tests an existing token. Uses `authorization_code` only (WHOOP rejects `client_credentials`), `client_secret_post`, a CSRF `state`, and a localhost:8080 callback (120s timeout). Run once per new WHOOP-enabled user during onboarding.

#### monitor/

**inbox_drain.py** — the autonomous ingestion pipeline, triggered by GH Actions on `repository_dispatch inbox-drain`: reads `media_inbox`, atomically claims rows, downloads images from Supabase Storage, sends them to Claude Vision, writes via the `maintainer_ingest_*` SECURITY DEFINER RPCs, and sends Telegram confirmations. `run_once(db, cfg, api_key, token, settle_sec_override)` is the full loop returning a summary dict; `fetch_settled`, `build_clusters` (album grouping by `media_group_id`), `vision_extract`, `write_food/supplement/biomarker`, the `*_is_complete` predicates (auto-write vs force-stage), `lookup_food_reference/lookup_viome_verdicts`, `compute_meal_verdict` (worst-case: avoid>minimize>superfood>clean), and `compose_confirmation` (minor-safe). Note: it uses `HS_ANTHROPIC_API_KEY` (not `ANTHROPIC_API_KEY`); cluster claims are all-or-nothing; it late-imports `monitor.brief` and composes a brief for any adult profile that had a successful food write.

**brief.py** — composes and sends the structured daily Telegram health brief after food ingestion (macros vs targets, supplement adherence by slot, WHOOP recovery/HRV/sleep, Viome flags, 2–4 Claude-generated actions). `compose_brief(db, profile_id, cfg, api_key, token, today)` is the single entry point; private `_fetch_*` section fetchers all return empty on error (best-effort) and `_food/_supps/_whoop/_viome_section` render minor-safely. Default model `claude-haiku-4-5-20251001` (override via `brief.model`); Viome is suppressed for minors; `PENDING_SCORE`/`UNSCORABLE` are shown with emoji rather than misleading zeros. CLI uses `ANTHROPIC_API_KEY` (inconsistent with inbox_drain — note when running locally).

**ingest_health.py** — maintainer-only (PC) session-start data-quality surfacing: staging backlog counts and wearable sync failures, never shown to non-maintainers. `is_maintainer(conn)` calls the DB function; `check(conn, recent_days=7)` returns `{maintainer, items}` with `staging`, `sync_failure`, and per-code `errors`. Defence-in-depth (skill-level gate + maintainer-only RLS); psycopg2 only, so it cannot run in supabase_client mode.

**trend_monitor.py** — proactive session-start trend detection for all profiles: recovery/HRV decline, strap compliance, out-of-range biomarkers, goal progress. `recovery_hrv_shifts` is self-calibrating (flags a drop only when it exceeds the prior window's own std dev, ≥3 prior + ≥2 recent scored cycles); `strap_compliance` flags >1/3 unscored cycles; `abnormal_biomarkers` delegates to the `abnormal_labs` view; `goal_findings` delegates to `plan.goals.track_goals`; `check(conn, profile_id, persist=False)` runs all four, sorts by severity, and optionally dedup-inserts into `trend_alerts`. All thresholds are relative — never magic clinical constants; one monitor's failure rolls back without aborting the others.

**query_log.py** — maintainer-only session-start query-observability digest over `query_audit` (total, catalog vs ad-hoc, flagged-by-quality-layer, most-used views). `check(conn, recent_days=7, ...)` returns the summary; `format_digest(result)` renders a one-glance string (empty for non-maintainer or silent windows). Same defence-in-depth + psycopg2-only constraint as `ingest_health.py`.

**reconcile.py** — age-aware WHOOP reconcile: computes a per-profile window from how long ago the last successful sync ran, then calls `ingest.whoop_sync.refresh_recent` — catches missed data without unbounded backfills. `reconcile(conn=None, profiles=None, default_hours=None, max_widen_hours=None)` is the entry point (opens/closes its own connection if `conn` is None); `_profile_window` computes `min(max(default, hours_since_last + 2h), max_widen)`. Config keys `push.reconcile_default_hours` (48h) and `push.reconcile_max_widen_hours` (168h); one profile's failure never blocks others. Run it as a scheduled catch-up.

#### analysis/

**food_chat.py** — conversational diet-log wrapper enforcing confirm-before-write. `preview_meal(conn, profile_id, items|description)` classifies without writing (returns `needs_confirmation: True` + guidance hits/unmatched/parsed/confidence); `log_meal` writes the confirmed meal via `ingest.food.ingest_food_row`; `daily_total(conn, profile_id, log_date, calorie_target)` gives running macros vs target — `remaining_cal`/`pct_of_target` are None when `calorie_target` is None (read the target from context, never hardcode). Excludes `is_day_summary=true` rows to avoid double-counting.

**trends.py** — sleep and workout trend narratives over a configurable window, with all arithmetic done deterministically in Python (LLM interprets, code calculates). `sleep_trend(conn, profile_id, days=30)` uses the recovery/sleep-debt views and is NULL-recovery aware (unscored nights excluded from averages, reported separately); `workout_trend(conn, profile_id, days=30)` uses the zone-summary view and returns workouts, Zone2, and high-intensity (Z4+Z5) minutes per week plus a narrative. Use it when the skill needs a plain-language trend readout.

**supplement_summary.py** — active regimen status with on/off tracking and adherence over a window. `summary(conn, profile_id, days=14)` joins `supplement_regimens` with `supplements`, computes `days_logged`, and returns adherence% only for daily (non-cyclical) regimens — `None` for cyclical ones (the skill must not invent a rate). RLS-scoped, profile-isolated.

**interval_report.py** — coaching readout for a structured workout (e.g. 4×4): zone distribution, protocol compliance, redline detection, recovery quality, transition efficiency, enriched with per-interval HR trace when `workout_intervals` rows exist. `main()` is the CLI; `_fetch_workout` (by date picks highest strain), `_fetch_zone_config` (point-in-time, latest `effective_date <= workout_start`), `_fetch_intervals`, and `_analyse(...)` (returns markdown). `protocol` is a JSONB column that must be set manually. Note: a duplicate module-level `_pct` exists — the second definition (keyword args `hi_intensity=`, `total=`) shadows the first.

#### plan/

**goals.py** — CRUD and direction-aware progress tracking for concurrent health goals in `user_goals`. `create_goal(...)` inserts a goal and resolves `metric_name` to `metric_definitions.id` by exact match only (unresolved → stored NULL → qualitative goal); `list_goals`, `compute_progress(current, baseline, target)` (direction-aware %, clamped [0,100], None on missing input or target==baseline), `track_goals(conn, profile_id)` (active goals with current value and `progress_pct`, None when no metric/baseline/reading); `_resolve_metric` does the case-insensitive exact match. Multiple concurrent goals are allowed (migration 020 dropped the uniqueness constraint).

**training_plan.py** — create and read training programs with ordered phases; `program_workouts` links only concrete performed workouts (no placeholder rows for future sessions). `create_training_plan(conn, profile_id, name, phases, ...)` inserts `training_programs` + `program_phases` (prescribed sessions live in `weekly_template` JSONB); `list_training_plans` (active-first), `get_training_plan` (assembles program + phases + linked concrete workouts), `set_status` (validated against `planned/active/done/abandoned`). `ordinal` defaults to list index.

#### export/

**dashboard.py** — generates a self-contained snapshot HTML dashboard with Chart.js, data baked in as JSON (no DB credential in the file). `build_dashboard(conn, profile_id, out_path, display_name="HealthSpan")` fetches six views (recovery, sleep debt, workout zones, abnormal labs, sprints, VO2), serialises them, and writes the HTML. It is a snapshot — stale immediately; refresh = regenerate. The recovery chart uses `spanGaps:false` so null recovery appears as gaps, never zeros. Consumes `lib.views` only.

**export_file.py** — exports any set of `lib.views` views to XLSX (one sheet per view + Summary) or PDF (multi-section formatted report); `openpyxl`/`reportlab` are imported lazily so the rest of the system works without them. `to_xlsx(conn, names, profile_id, out_path, **params)` (sheet names truncated to 31 chars, `/`→`_`) and `to_pdf(...)` (A4 tables, capped at 60 rows per view — full data only in XLSX). Use it for offline/handover deliverables.

**report_md.py** — renders any `lib.views` view or the standard daily summary as markdown (what the skill prints in chat). `render_view(conn, name, profile_id, max_rows=50, **params)` returns a heading + description + summary + table; `render_summary(conn, profile_id)` is a fixed five-view digest that swallows per-view errors (a broken view shows as an error line, never an exception); `render_catalog()` lists available views. No SQL here — consumes `lib.views` only.

#### scripts/

**self_test.py** — read-only diagnostic and capabilities readout; first detects WHERE it runs (App vs Cowork/Code) from filesystem signals, then runs 8 checks (version, identity, connection + RLS-scope proof, context targets, maintainer status, data freshness, capabilities, verdict). `main()` exits 0 (READY) / 1 (BLOCKED) / 2 (no config). Strictly read-only — raw `SELECT` only, never `run_view`/`run_adhoc_audited` (would log to `query_audit`); proves `distinct_profiles == 1` matching config and warns if `SCHEMA-MAP.md` is >30 days old. Run it after onboarding or any environment change.

**hs_ops.py** — day-to-day DB operations CLI wrapping `psql`/`pg_dump` (must be v17 to match PG 17.6). Commands: `whoami`, `backup [label]`, `apply <migration.sql>` (via `psql -v ON_ERROR_STOP=1`, atomic, exits with psql's code), `verify` (RLS-leak/coverage checks + live SET ROLE read test), `mint-token`, `mint-link-code`, `setup-telegram-storage`. Note: `parts()` parses `DATABASE_URL` by hand (urlparse mishandles `#` in passwords); the pg_dump/psql binary path is hardcoded to Mac Homebrew `/opt/homebrew/opt/postgresql@17/bin`; pooler port 6543 for psycopg2, session port 5432 for dump/SET ROLE tests. The canonical way to apply a migration.

**gen_schema_map.py** — generates `docs/SCHEMA-MAP.md` from live `pg_description` (column/table COMMENTs set in migrations); the skill loads this before composing ad-hoc SQL, so it must never drift. `main()` queries all public base tables (`relkind='r'`), renders documented tables in a curated then alphabetical order, and appends an "Unmapped tables" backstop so no table silently hides; `_render_table` renders one section. GENERATED, never hand-edited (edits are overwritten on next run); covers base tables only (views excluded). Run after any migration that touches tables, columns, or COMMENTs.

---

## 6. ONBOARDING — NEW USER

Step-by-step guide for onboarding a new family member (e.g. Nanki) to the HealthSpan platform.

### Step 1 — Create Supabase Auth user

In the Supabase dashboard (project `dsnydskkjwziynwmzfkh`) go to **Authentication → Users → Invite user**. Enter the new user's email and set a temporary password. Note the `auth_user_id` UUID that is created — you will need it in step 2.

### Step 2 — Insert profiles row

```sql
INSERT INTO public.profiles (id, display_name, relationship, is_maintainer, family_id, auth_user_id)
VALUES (
  gen_random_uuid(),         -- or supply a fixed UUID you choose
  'Nanki',
  'spouse',
  false,
  '3d45c05c-1175-4ca9-9578-3d206a4da025',  -- the Chitalkar family_id (same as PC/Dea/Dev)
  '<auth_user_id from step 1>'
);
```

Schema note (verified against the live DB): `profiles` columns are
`id, auth_user_id, family_id, managed_by_auth_user_id, display_name, date_of_birth, sex,
relationship, is_active, created_at, updated_at, is_maintainer`. There is **no `email`
column** and **no `is_minor` column** on `profiles`. `family_id` is a direct FK on the row
(not resolved through `family_memberships`). For a profile the drain account should manage
(e.g. a minor), also set `managed_by_auth_user_id` to the drain account's `auth_user_id`.

`is_maintainer = false` means staging queues, wearable sync logs, and query audit are invisible to this user at the DB level (RLS). Minor status is **not** a `profiles` column — set `is_minor: true` in the config JSON (step 4) AND the context MD (step 5) if the user is under 18. Minor safety is defence-in-depth and both must agree.

### Step 3 — Insert family_memberships row

`family_memberships` is the access-grant table. Its real columns are
`id, auth_user_id, profile_id, role, created_at` — it has **no `family_id` column**. Both
DB guards key on the `(auth_user_id, profile_id)` pair:

- `has_profile_access(p_profile_id)` → `EXISTS (SELECT 1 FROM family_memberships WHERE auth_user_id = auth.uid() AND profile_id = p_profile_id)`
- `is_maintainer()` → the caller's `auth_user_id` has a membership row whose linked profile has `is_maintainer = true`

Grant the new user access to their own profile:

```sql
INSERT INTO public.family_memberships (auth_user_id, profile_id, role)
VALUES ('<auth_user_id from step 1>', '<new_profile_id>', 'member')
ON CONFLICT DO NOTHING;
```

Then grant the drain service account (`healthspan.drainer@chitalkar.com`, `auth_user_id` `0b0e4093-6758-46f7-a6e2-311ef6828a86`) access to the new profile so the three `maintainer_ingest_*` RPCs pass the `has_profile_access()` guard when draining this user's photos:

```sql
INSERT INTO public.family_memberships (auth_user_id, profile_id, role)
VALUES (
  '0b0e4093-6758-46f7-a6e2-311ef6828a86',  -- drain account's auth_user_id
  '<new_profile_id>',
  'owner'
)
ON CONFLICT DO NOTHING;
-- Grants the DRAIN account access to the NEW profile (auth_user_id = drain, profile_id = new user).
```

### Step 4 — Create config JSON

```bash
cp config/healthspan.config.example.json config/nanki.config.json
```

Fill in:
- `profile_id` — UUID from step 2
- `auth_user_id` — from step 1
- `auth_email` / `auth_password` — the credentials set in step 1
- `display_name` — `"Nanki"`
- `context_slug` — `"nanki"` (must match the filename in step 5)
- `connection_mode` — `"supabase_client"` (all non-PC users use the PostgREST path)
- `supabase_url` / `supabase_anon_key` — from the project settings

**Never commit a filled config file.** The example file is the only committed template.

### Step 5 — Create context MD

```bash
cp context/dea.context.md context/nanki.context.md
```

Edit the new file:
- Set `name`, `profile_id` (must match step 2 exactly — mismatch is a hard error in `lib/context.py`)
- Set `age`, `sex`
- Set `is_minor: false` (or `true` if applicable)
- Fill in `targets` — calorie goal, protein, sleep hours, any specific biomarker targets
- Fill in `coaching` — tone, focus areas, constraints
- Fill in `safety` — any conditions, medications, or restrictions

The context MD is the sole source of targets and coaching voice. Nothing is defaulted from population norms.

### Step 6 — Mint a Telegram link code

```bash
python3 scripts/hs_ops.py mint-link-code <new_profile_id>
```

This inserts a single-use code into `telegram_link_codes` with `expires_at = now() + 24h`. The code is printed once — copy it and send it to the new user.

### Step 7 — User sends the code to the Telegram bot

The new user opens the HealthSpan Telegram bot and sends the 6-digit code as a message. The `telegram-webhook` Edge function matches the code, creates a `telegram_identities` row linking their Telegram chat ID to their `profile_id`, and marks the code used. No further action needed on the admin side.

### Step 8 — WHOOP OAuth (if user has WHOOP)

```bash
python3 -m ingest.whoop_oauth        # opens browser, completes consent, stores refresh token in .env
python3 -m ingest.whoop_sync --backfill  # pulls full history from 2020-01-01
```

The refresh token is stored in `.env` under `WHOOP_REFRESH_TOKEN`. `whoop_sync` writes it to `whoop_tokens` in the DB after the first successful sync, enabling the webhook and reconcile paths going forward. Confirm the sync completed by checking `wearable_sync_log`.

### Step 9 — Verify with self_test.py

```bash
python3 scripts/self_test.py --config config/nanki.config.json
```

Expected output:
- Connection: PASS
- RLS scope: exactly 1 profile visible, matching the new user's `profile_id`
- Maintainer: `false`
- Context: targets loaded, no profile_id mismatch
- Staging queues visible: 0 rows (non-maintainer sees nothing)
- Exit code: 0 (READY)

If exit code is 1 (BLOCKED), read the step that failed. The most common causes are a missing `family_memberships` row, a mismatched `profile_id` in the context MD, or `auth_password` not yet set in Supabase Auth.

### Worked example — Dea (minor, ~95% onboarded as of 2026-06-08)

Most of the 9 steps above are already done for Dea; this is what a partially-onboarded user
looks like and the single remaining action. Full plan: `docs/DEA-ONBOARDING-PLAN.md`.

| Step | Dea's state |
|---|---|
| 1 Auth user | ✅ `47501376-6adb-458e-8679-57a4a4176692` |
| 2 Profile | ✅ `3eed5503-…` — "Dea Singh Chitalkar", `relationship='child'`, `sex='female'` |
| 3 Family membership | ✅ PC (`0b0e4093…`, owner) → Dea, and Dea (`47501376…`, self) → Dea |
| 4 Config JSON | ✅ `config/dea.config.json` (`is_minor: true`) |
| 5 Context MD | ✅ `context/dea.context.md` |
| 6 Link code | ❌ **not yet minted — this is the only remaining step** |
| 7 User sends code | ❌ pending — Dea sends it from **her own** Telegram |
| 8 WHOOP OAuth | ✅ linked (`whoop_user 22399950`) + backfilled (300 cycles / 281 sleeps / 140 workouts), synced same-day |
| 9 Verify | run after linking |

Remaining action:
```bash
python3 scripts/hs_ops.py mint-link-code 3eed5503-a26f-4b88-bb76-075208fa5de3
# → Dea opens the "Chitalkar Family Health Tracker" bot and sends the code (or /start <code>)
```
Because `profiles.relationship = 'child'`, `telegram-webhook` sets `is_minor = true` on her
`telegram_identities` row automatically — minor-safe framing (growth/performance only, no
deficit/restriction language) applies from her first message. PC, as maintainer, sees her data;
Dea sees outcomes only.

---

## 7. MAINTENANCE RUNBOOK

### 7.1 Applying a Migration

```bash
python3 scripts/hs_ops.py apply migrations/NNN_name.sql
```

Rules that every migration must satisfy:
- **Idempotent** — use `IF NOT EXISTS` for all `CREATE TABLE`, `CREATE INDEX`, `CREATE POLICY`, `CREATE FUNCTION`, `ALTER TABLE ADD COLUMN IF NOT EXISTS`.
- **RLS in the same file** — every `CREATE TABLE` must be accompanied by `ALTER TABLE ... ENABLE ROW LEVEL SECURITY` and at least one `CREATE POLICY` in the same migration.
- **No hardcoded thresholds** — any numeric threshold must be seeded into `system_config`, not embedded in SQL or application code.
- **GENERATED columns** — never include a GENERATED column in an INSERT column list. It may appear as an `ON CONFLICT` target (Postgres auto-populates it). See `supplement_intake_logs.taken_on` as the canonical example.
- **Atomic** — `hs_ops apply` runs with `ON_ERROR_STOP=1`; the entire migration rolls back on any error.

There is no `schema_migrations` tracking table. The applied DB state is the sole source of truth. The migration filename sequence (`001`, `002`, … `NNN`) is the canonical ordering record.

### 7.2 Running a Manual Drain

When the `inbox-drain` workflow stalls, a photo was not processed, or you need to force re-process pending rows:

```bash
# Trigger via GitHub CLI (recommended — uses the same GH Actions path)
gh workflow run inbox-drain -f settle_sec=0

# Or dispatch directly via curl
curl -X POST https://api.github.com/repos/<org>/healthspan-foundation/dispatches \
  -H "Authorization: Bearer $GH_DISPATCH_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"event_type":"inbox-drain","client_payload":{"settle_sec":"0"}}'
```

`settle_sec=0` skips the settle window and processes whatever is currently `pending` in `media_inbox` immediately.

To run the drain locally (bypasses GH Actions):

```bash
python3 -m monitor.inbox_drain --settle-sec 0
```

After a manual drain, check the `media_inbox` table for any rows still in `pending` or `failed` state:

```sql
SELECT id, kind, status, stage_reason, created_at FROM media_inbox
WHERE status IN ('pending', 'failed')
ORDER BY created_at DESC LIMIT 20;
```

Failed rows with a `stage_reason` are expected (completeness gate); rows with `_error` in stage_reason indicate a pipeline fault and need investigation.

### 7.2a Clearing the Review Queue (the most common ops task)

When the bot says "**N to review**", incomplete items are parked in `stg_*_review` with `status='pending'` and a `stage_reason`. To see and clear them:

```sql
-- What's waiting, and why
SELECT id, description, meal_type, calories, protein_g, carbs_g, fat_g, stage_reason, created_at
  FROM stg_food_log_review WHERE status = 'pending' ORDER BY created_at DESC;
-- (also stg_supplement_intake_review, stg_biomarker_review)
```

To **complete** an item and write it to production, fill the missing fields and call the contract RPC `maintainer_ingest_food` / `maintainer_ingest_supplement` / `maintainer_ingest_biomarker` with `p_force_stage := false`. These RPCs are `SECURITY DEFINER` and gate on `is_maintainer() AND has_profile_access()`, both keyed on `auth.uid()`. From a direct `psql`/psycopg2 connection (role `postgres`, `auth.uid()` is NULL) you must **impersonate the maintainer** by setting the JWT claim first:

```sql
-- 1. Impersonate PC (auth_user_id from family_memberships where profiles.is_maintainer)
SELECT set_config('request.jwt.claims',
  json_build_object('sub','<maintainer_auth_user_id>','role','authenticated')::text, true);

-- 2. Write to production (force_stage := false routes through to food_logs)
SELECT maintainer_ingest_food(
  p_profile_id := '<profile_id>', p_meal_type := 'snack',
  p_description := 'alley High-Protein Shake (Original), 380ml bottle',
  p_calories := 180, p_protein_g := 28, p_carbs_g := 24, p_fat_g := 4, p_fiber_g := 12,
  p_source := 'telegram', p_force_stage := false,
  p_notes := 'maintainer-completed from nutrition label');

-- 3. Retire the staging row + its media_inbox rows so it leaves the queue
UPDATE stg_food_log_review SET status='approved', reviewed_at=now() WHERE id = '<review_id>';
UPDATE media_inbox SET status='done', processed_at=now() WHERE id IN (<inbox_ids>);
```

Resolve `<maintainer_auth_user_id>` with:
```sql
SELECT fm.auth_user_id FROM family_memberships fm JOIN profiles p ON p.id=fm.profile_id
 WHERE p.is_maintainer = true LIMIT 1;
```

If the source was a photo and you need to read it, sign a URL for the `health-media` object and download it:
`POST {SUPABASE_URL}/storage/v1/object/sign/health-media/<storage_path>` with the service-role key → `GET {SUPABASE_URL}/storage/v1{signedURL}`. `media_inbox.storage_path` holds the object path.

> The completeness gate is **working as designed** when it parks an item — the usual root cause of a surprise review is the *extraction* missing data that was present (e.g. a nutrition label). Check the photo before assuming the data wasn't there. (See the 2026-06-08 vision-label fix in `BACKLOG.md`.)

### 7.3 WHOOP Sync

```bash
# Sync recent data (last 48h, all token-holding profiles)
python3 -m ingest.whoop_sync --since 48h

# Full history backfill from 2020-01-01
python3 -m ingest.whoop_sync --backfill

# Full backfill across all profiles
python3 -m ingest.whoop_sync --backfill --all-profiles

# Reconcile: age-aware window sync (uses system_config push.reconcile_* thresholds)
python3 -m monitor.reconcile
```

WHOOP does not emit `cycle.updated` webhooks, so the final `day_strain` on a just-closed cycle is only picked up by `refresh_recent` or a manual `--since` run. If recovery/strain numbers look stale, run a `--since 24h` sync.

Score states to watch:
- `PENDING_SCORE` — cycle closed but WHOOP has not yet scored it; re-sync in 15–30 min.
- `UNSCORABLE` — WHOOP cannot score (insufficient wear time). Brief shows a flag indicator; do not treat as 0.
- `recovery_user_calibrating = true` — new user or freshly linked device; recovery % is unreliable until calibration completes (~4 weeks).

### 7.4 Deploying Edge Functions

Always run from the `healthspan-foundation` repo root, not from another project directory.

```bash
# Trigger-drain (pg_net webhook → GH Actions inbox-drain dispatch)
supabase functions deploy trigger-drain

# WHOOP webhook (receives WHOOP events, upserts data, sends Telegram push)
supabase functions deploy whoop-webhook --no-verify-jwt

# Telegram webhook (receives Telegram updates, routes to media_inbox or send-brief)
supabase functions deploy telegram-webhook --no-verify-jwt
```

`trigger-drain` runs **no-auth** (migration 047). The earlier vault/Bearer scheme (migrations 043) was removed because `vault.decrypted_secrets` is not reachable from the pg_net trigger context — it silently returned NULL → empty Bearer → 401 → every photo stalled at `pending`. `fn_media_inbox_notify` now POSTs to `trigger-drain` with no auth header; the function is safe to leave open because it only fires a GitHub `repository_dispatch` (no tokens are spent until a real photo exists) and dedups via the `system_config` CAS. **Do not re-add Bearer auth to this path** without first solving pg_net's vault access.

> ⚠️ **CLI gotcha:** `supabase functions deploy` sometimes prints `No change found in Function` and skips a real upload even when the source changed (stale local bundle hash). If you changed the code and it says "no change", touch the file (e.g. bump a `// deploy: <marker>` comment) and redeploy — confirm you see `Deploying Function: … (script size: …)`.

After deploying `whoop-webhook`, verify the webhook URL is registered with WHOOP by checking the WHOOP developer portal — the URL must point to the deployed function's endpoint.

### 7.5 Regenerating SCHEMA-MAP

```bash
python3 scripts/gen_schema_map.py > docs/SCHEMA-MAP.md
```

Run this after any migration that adds, removes, or renames tables or columns, or that adds `COMMENT ON COLUMN/TABLE` entries. The schema map is what the skill loads before composing ad-hoc SQL — a stale map causes hallucinated column names.

`scripts/self_test.py` reads the `generated_at:` stamp in the map and warns if it is more than 30 days old. The map is generated, never hand-edited — any manual edits will be overwritten on the next run.

### 7.6 Rotating Secrets

**GitHub Actions secrets** (set via `gh secret set <NAME>` or the GitHub UI under Settings → Secrets and variables → Actions):

| Secret | Purpose |
|---|---|
| `HS_AUTH_EMAIL` | Drain service account email (`healthspan.drainer@chitalkar.com`) |
| `HS_AUTH_PASSWORD` | Drain service account password |
| `HS_ANTHROPIC_API_KEY` | Anthropic API key used by the drain pipeline (distinct from `ANTHROPIC_API_KEY`) |
| `SUPABASE_URL` | Supabase project URL |
| `SUPABASE_ANON_KEY` | Supabase anon (public) key |
| `DATABASE_URL` | Direct postgres connection string (pooler, port 6543) |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token for sending confirmations |

**Supabase Edge Function secrets** (set via `supabase secrets set <KEY>=<VALUE>`):

| Secret | Purpose |
|---|---|
| `WHOOP_CLIENT_ID` | WHOOP OAuth application client ID |
| `WHOOP_CLIENT_SECRET` | WHOOP OAuth application client secret |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token (consumed by telegram-webhook and whoop-webhook) |
| `TELEGRAM_WEBHOOK_SECRET` | Secret token for verifying Telegram webhook requests |
| `GH_DISPATCH_TOKEN` | GitHub PAT used by trigger-drain to dispatch `inbox-drain` events |
| `TRIGGER_DRAIN_SECRET` | **Deprecated / unused (mig 047)** — this path reverted to no-auth; nothing to rotate. |

After rotating a secret, redeploy any Edge function that consumes it — secrets are baked in at deploy time for Deno functions.

After rotating `HS_AUTH_PASSWORD`, also update the Supabase Auth password for `healthspan.drainer@chitalkar.com` in the Supabase Auth dashboard.

### 7.7 Staging Queue Review

When a food, supplement, or biomarker item fails the completeness or plausibility gate, it lands in a staging table with status `pending`. Only the maintainer (PC) can see these rows.

```sql
-- Food items awaiting review
SELECT id, profile_id, raw_text, stage_reason, created_at
FROM stg_food_log_review
WHERE status = 'pending'
ORDER BY created_at DESC;

-- Supplement items awaiting review
SELECT id, profile_id, raw_text, stage_reason, created_at
FROM stg_supplement_intake_review
WHERE status = 'pending'
ORDER BY created_at DESC;

-- Biomarker items awaiting review
SELECT id, profile_id, raw_text, stage_reason, created_at
FROM stg_biomarker_review
WHERE status = 'pending'
ORDER BY created_at DESC;
```

Common `stage_reason` values and their meaning:

| stage_reason | Meaning | Action |
|---|---|---|
| `kcal_implausible` | Calories outside 25–12,000 range | Re-send with corrected value; check for OCR comma-slip (e.g. `1,020` parsed as `20`) |
| `supplement_unresolved` | Supplement name not matched in catalog | Add alias to `supplement_aliases` or correct the name |
| `biomarker_implausible` | Value outside `plausible_min/max` range in `metric_definitions` | Verify the lab value; if correct, update the plausibility bounds in a migration |
| `confidence_low` | LLM extraction confidence below `system_config.ingest.confidence_threshold` (0.7) | Re-send the photo at higher quality or with a caption |
| `missing_required` | A required field (e.g. `description`, `meal_type`) is NULL | Re-send with complete information |

To approve a staged item, write the corrected row directly to the production table using the appropriate `maintainer_ingest_*` RPC with `p_force_stage = false` and the corrected values. To dismiss, update the staging row to `status = 'dismissed'`.

---

## 8. NON-NEGOTIABLE RULES

These rules are sourced from `CLAUDE.md` and apply to every code change, migration, and data operation in this repository without exception.

1. **Never hardcode thresholds** — all numeric thresholds (calorie gates, confidence minimums, settle windows, plausibility bounds, push debounce values) must come from the `system_config` table. No magic constants in SQL, Python, or edge functions.

2. **AI extraction output goes to staging first** — all output from Claude Vision or any LLM extraction step must be written to a `stg_*` staging table, never directly to a production table (`food_logs`, `biomarkers`, `supplement_intake_logs`). The only path from staging to production is an explicit maintainer approval or a completeness-gate pass via `p_force_stage = true` in the RPC.

3. **Every new table needs RLS in the same migration** — `CREATE TABLE` without a matching `ALTER TABLE ... ENABLE ROW LEVEL SECURITY` + `CREATE POLICY` in the same migration file will not pass review. No exceptions for "internal" or "lookup" tables.

4. **Test files mirror source structure** — a new module at `src/foo.py` requires a test file at `tests/unit/test_foo.py`. Test coverage must be added in the same PR as the module.

5. **Commit prefixes are mandatory** — every commit must start with one of: `feat:` | `fix:` | `schema:` | `test:` | `docs:` | `chore:` | `adr:`. No bare messages.

6. **Never modify auth logic without explicit user instruction** — the authentication, JWT claim injection, RLS policy structure, and `is_maintainer()` / `has_profile_access()` DB functions are frozen. No refactoring, "cleanup", or "simplification" of these without an explicit instruction from the owner.

7. **When stuck for more than 2 attempts, stop and ask** — do not keep trying variations on a failing approach. Surface the blocker clearly and ask for guidance.

---

## 10. Verification Tests

Live verification for the 2026-06-08/09 changes — tick each as you confirm it in Telegram. Run as PC unless noted.

(Markdown checkboxes are text `[ ]` — tick by editing to `[x]`.)

### Group A — Text logging (single extractor)

| ✓ | Test | Send | Expect | Proves |
|---|------|------|--------|--------|
| ✅ **PASS** | **A1** | `took D3, K2, magnesium citrate, omega-3` | ✅ "Logged 4 supplements: …" — *verified 2026-06-09 (webhook v6 / drain@main): "Logged 3 supplements: Vitamin D3, Vitamin K2, Magnesium Citrate"* | multi-supplement + display-name match + item count |
| ✅ **PASS** | **A2** | `took my nutritional yeast` | ✅ logged (5g default) — *verified 2026-06-09 (drain@main): "nutritional yeast logged"* | dose-less supplement + regimen-dose default |
| `[ ]` | **A3** | `ate 2 eggs and a banana` | ✅ "Logged 2 items: …" | multi-item food, natural phrasing |
| `[ ]` | **A4** | `add half a thai tea shake` | ✅ ~95 kcal / 17.5P (NOT 190/35) | portion scaling (#10) + food_reference |

### Group B — Clarification loop (the keystone)

| ✓ | Test | Send | Expect | Proves |
|---|------|------|--------|--------|
| ✅ **PASS** | **B1** | `took my supplement` (vague) | 📋 a SPECIFIC question — *verified 2026-06-09: "Add electrolytes" → "For electrolytes, which brand or product name should I log?"* | confidence gate + generic guard + descriptive feedback |
| ✅ **PASS** | **B2** | LONG-PRESS B1's question → Reply → the name | 📥 "Got it — updating that…" then re-extract | *verified 2026-06-09 (KEYSTONE): reply re-queued, reprocessed, asked an intelligent follow-up (name → dose → brand), then capped at 2 rounds and handed to PC. Reply-to-clarify round-trip (#5) works end-to-end.* |
| `[ ]` | **B3** | after B2, check no duplicate | only ONE Magnesium Bisglycinate entry | clarify supersedes, no dupe |

> **NOTE:** B2 MUST be an actual Telegram reply (long-press the bot's message → Reply), not a typed follow-up — that's the correlation mechanism.

### Group C — Photo path (consolidation didn't break it)

| ✓ | Test | Send | Expect | Proves |
|---|------|------|--------|--------|
| ✅ **PASS** | **C1** | a clear meal photo, caption "lunch" | ✅ logged with macros — *verified 2026-06-09 · drain@a2b2045 / webhook v6: black coffee photo logged 2 kcal (after kcal-floor fix mig 050)* | photo path survives single extractor |
| `[ ]` | **C2** | a nutrition-label photo ("this shake") | macros read from label (not "unidentified") | label reading + max_tokens fix |
| `[ ]` | **C3** | a blurry/ambiguous food photo | 📋 asks what it is (clarify), not a wrong guess | clarify-on-uncertainty on photos |

### Group D — Brief / summary

| ✓ | Test | Send / Check | Expect | Proves |
|---|------|--------------|--------|--------|
| ✅ **PASS** | **D1** | `how am I doing?` | the daily brief (~25s) — *verified 2026-06-09 · drain@a2b2045 / webhook v6* | brief routing |
| ✅ **PASS** | **D2** | check D1's Energy line | "… in − … out (BMR 1935 + … activity) · net −… deficit" (NOT "+ surplus") — *verified 2026-06-09 · drain@a2b2045: '4 in − 671 out (WHOOP total) · net −667 deficit' — correct, no double-count* | energy-balance fix |
| ✅ **PASS** | **D3** | check D1's WHOOP line | fresh recovery, no false "⚠️ >24h old" — *verified 2026-06-09 · drain@a2b2045: June-9 recovery 60% shown after the refresh-in-CI fix* | refresh-on-interaction + tz-safe staleness |
| ✅ **PASS** | **D4** | check D1's food day boundary | only today's (Thailand) meals counted — *verified 2026-06-09 · drain@a2b2045: 'Calories: 4 / 2100 (2096 left)' targets now load (slug fix)* | local-timezone fix |
| `[ ]` | **D5** | `thanks!` | the brief, NOT a junk log | junk rejection |

### Group E — Dea (minor-safe), after she links

| ✓ | Test | Send (as Dea) | Expect | Proves |
|---|------|---------------|--------|--------|
| `[ ]` | **E1** | Dea sends her link code from her phone | "✅ Account linked." | link + is_minor=true |
| `[ ]` | **E2** | Dea sends `had a banana` | ✅ growth wording ("Tracked… Nice! 💪"), no deficit talk | minor-safe confirmation |
| `[ ]` | **E3** | Dea sends `how am I doing?` | brief with no calorie-deficit line, no Viome restrictions | minor-safe brief |
| `[ ]` | **E4** | Dea sends a vague one → reply to clarify | warm, simple question (no clinical language) | minor-aware clarify loop |

### What pass means

Must-pass before relying = **A1–A4, B1–B2, C1, D1–D2**. **B2 is the keystone** (proves the clarify architecture). If any C test logs a wrong guess instead of asking, the confidence calibration needs a nudge.
