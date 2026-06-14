# HealthSpan Foundation — System Manifest

<!-- Generated 2026-06-08. Sections 1–5 cover architecture, schema, and module internals.
     Sections 6–8 cover onboarding, maintenance operations, and non-negotiable rules. -->

> The **live Supabase DB is the source of truth for numbers**; the per-person context MD
> is the source of truth for targets, norms, and coaching voice. Generated against repo
> state with migration 071 applied (2026-06-14), skill v3.20.0.

---

## 1. SYSTEM OVERVIEW

### What it is

A transportable, multi-tenant, DB-backed **personal health intelligence platform** for
the Chitalkar household — currently **PC** (Parikshit Chitalkar, owner/maintainer) and
**Dea** (14F; `is_minor=false` — adult coaching framing, an explicit PC-authorized parent override
of the age default, commit `eb40c06`), with **Nanki** (PC's wife, 45F adult) ready to onboard when she is, and a
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
- **Two write surfaces, one database — PLAN vs TRACK.** The same Supabase DB is updated from
  both ends, by design:
  - **claude.ai skill (`SKILL.md`) = the PLANNING surface.** Used at a desk to *design*: build
    training sprints + `weekly_plan`/`daily_overrides`, set goals/targets, curate context, learn
    food/supplement references, run analyses. Writes sprints, goals, regimens, references, context.
  - **Telegram bot = the TRACKING surface.** Used on the phone to *record what actually happened*:
    food photos/text → drain, supplement + training-adherence ticks via the "📝 Update today" menu,
    WHOOP, the daily brief. Writes `food_logs`, `supplement_intake_logs`, `strength_logs`,
    `goals.adherence_log`, etc.
  - They share the same RLS-scoped tables, so a plan made in claude.ai immediately shapes the
    Telegram brief, and a tick in Telegram immediately updates what the skill reads next. Neither
    surface owns the data — the DB does.
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
| AI | Anthropic Claude API — **one model everywhere: `claude-sonnet-4-6`** (drain vision, clustering, brief, food-classify). `lib/models.HAIKU` stays available for a per-path `system_config` override (e.g. `brief.model`) but nothing uses it by default — see §2.6. |
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

### 2.0 Live Deployment — versions & deploy dates

What's actually running, and when each piece was last shipped. **Live source of truth:**
`supabase functions list` (edge `VERSION` = Supabase's deploy counter + `UPDATED_AT`),
`CHANGELOG.md` (skill), `migrations/` (schema). Re-run those to refresh this table.

| Component | Version | Last deployed (UTC) | What's live |
|---|---|---|---|
| **Skill** (Python: drain / brief / ingest) | **v3.20.0** | on push to `main` (GH Actions — no edge deploy) | food→supplement bridge (regimen supplements named in a food log auto-log as intakes → mig 069); food micronutrient check-in |
| **DB schema** | **mig 071** | 2026-06-14 | deep-scan cleanup: 070 threshold config keys (Rule #1), 071 Proten Thai Tea aliases; + 069 bridge flag, 067 view RLS leak fix, 068 nutrition keys |
| **telegram-webhook** | **v11** | **2026-06-12** | supp-menu UX (#26): named toast + slot count, slot-drill hint, `taken_on`→local-day; adherence ticks via `sprint_set_adherence` RPC (atomic); two-level "📝 Update today" menu + `/whoop` reconnect |
| **whoop-webhook** | recovery-v3 + reconnect-alert (Supabase #18) | 2026-06-11 15:39 | `recovery.updated` fix + debounced dead-token reconnect prompt |
| **whoop-oauth** | ticket-ONLY reconnect (Supabase #15) | 2026-06-14 | SECURITY: removed the unauthenticated `?profile_id=` consent path + legacy raw `state` (cross-profile token injection); consent now requires the signed one-time `?t=` ticket ONLY. `offline`-scope consent + token store + TG confirm |
| **trigger-drain** | no-auth (mig 047) (Supabase #9) | 2026-06-09 17:12 | pg_net → GH `inbox-drain` dispatch |

> Edge `VERSION` is Supabase's deploy counter (increments every deploy); the **v9 / recovery-v3 /
> ticket-reconnect** labels are the human marker in each function's top-of-file `// deploy:` comment.

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
| `direct_role` | PC / local / Cowork / Code | psycopg2 as `healthspan_app` against the pooler; skill injects `request.jwt.claims` (`{"sub":"<auth_user_id>","role":"authenticated"}`) + `SET ROLE authenticated` at session start; read-only session; RLS as defence-in-depth |
| `direct_role` + `privileged:true` | PC UNRESTRICTED maintainer bundle only (v3.16.0) | psycopg2 as **`postgres`**; sets the JWT claim but does **NOT** `SET ROLE authenticated`, so RLS is **bypassed** and DELETE/DDL across every profile is possible. Gated only by possession of the privileged credential (gitignored secret, injected per-bundle). ⚠️ The restricted guarantees below do not apply in this mode. |
| `supabase_client` | Dea / mobile / web / Telegram | supabase-py signs in with `auth_email` + `auth_password`; the client carries the person's JWT automatically (`auth.uid()` → RLS scopes). Non-maintainer writes go through `ingest/self_write.py` (direct insert of own rows), not `maintainer_ingest_*`. |

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
  `media_inbox`. Text-only messages ALSO enqueue to `media_inbox` (`kind='unknown'`,
  `storage_path=null`) — no regex routing; the drain's LLM classifies each as a log or a
  brief request and composes briefs inline (LLM-routed text, 2026-06-08). Replies to a
  clarify prompt re-queue the original item with `[clarification: …]` (staged-or-logged
  supersede, migs 049/054/055). `is_minor` changes ack/push wording.
- **"📝 Update today" two-level menu (v9, 2026-06-11):** the brief carries ONE button
  (`menu:<date>`); the `callback_query` handler is a small router (all rendered server-side from
  fresh data each tap): `menu:`→ top menu (training toggles `tick:<sprintId>:<date>:<activity>` +
  supplement slot buttons `slot:<date>:<slot>`); `slot:`→ that slot's individual pills
  `supp:<date>:<suppId>`; `supp:`→ toggle the supplement for today (append-only: untake voids today's
  non-voided intakes, take un-voids/inserts a `telegram` row) + re-render; `back:`/`close:`→ up/collapse.
  Training `tick:` sets `goals.adherence_log[date][activity]`. Every op is scoped to the tapping chat's
  own profile (active identity + explicit profile_id; service_role bypasses RLS). The message-ingestion
  path is untouched. ⚠️ requires the bot's `allowed_updates` to include `callback_query`.
- **`/whoop` command (v3.18.0):** an active identity sending `/whoop` (or "reconnect whoop") gets a
  one-tap WHOOP reconnect button (`sendReconnectPrompt`, force-past-debounce) — intercepted before the
  drain enqueue. See `whoop-oauth` for the rest of the flow.
- **Additional secrets:** `TELEGRAM_BOT_TOKEN` (download files + send replies),
  `TELEGRAM_WEBHOOK_SECRET` (verify inbound signature), and optionally
  `ROUTINE_TRIGGER_URL` + `ROUTINE_BEARER` (fire the legacy Routine drain — debounced
  300s fallback). (`GH_DISPATCH_TOKEN` is no longer used here — the old text →
  `send-brief` dispatch path was removed 2026-06-08; only `trigger-drain` dispatches.)
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
- **Deployed:** v3 marker / Supabase v14, 2026-06-10 — **recovery.updated UUID fix
  (BACKLOG #19)**. The v2 recovery webhook `id` is the **sleep UUID** (v1 sent cycle ids);
  the handler now resolves it via the `/v2/recovery` collection (keyed `sleep_id`) and
  fetches the integer `cycle_id`. Also fixed the reversed recovery-for-cycle path
  (`/v2/recovery/cycle/{id}` → `/v2/cycle/{id}/recovery` — the old shape 404'd silently,
  so webhook-sourced cycle rows never carried recovery fields). End-to-end verified with a
  signed synthetic event: `webhook:recovery.updated` success + first-ever `recovery_landed`
  push. Before the fix: 110 failed recovery webhooks/week, zero recovery pushes ever.

#### `whoop-oauth`
- **verify_jwt:** `false`. No inbound JWT — opened by a human in a browser. **Authorisation is the
  signed one-time ticket itself** (the `state` is `t.<ticket>`, validated/consumed server-side).
- **SECURITY (2026-06-14):** the old manual `GET ?profile_id=<uuid>` consent path and the legacy raw
  `state` callback were REMOVED. They had no auth, so any unauthenticated caller could consent with
  THEIR WHOOP account against ANYONE's `profile_id` and the callback's `storeTokens()` (service_role,
  RLS-bypassing) would bind the attacker's tokens to the victim. **Consent now starts ONLY from a
  `?t=<ticket>` link minted by an authenticated Telegram `/whoop` tap** (mig 065 `whoop_oauth_codes`,
  one-time + expiring). Callback accepts ONLY `state=t.<ticket>`; anything else → 400.
- **Purpose:** OAuth 2.0 consent (offline scope → refresh token), exchange, and token store.
  Entry `GET ?t=<ticket>` → validate ticket (peek, not consume) → redirect to WHOOP consent with
  `state=t.<ticket>`. Callback `GET ?code=...&state=t.<ticket>` consumes the ticket **single-use**,
  exchanges the code, fetches the WHOOP `user_id` from `/v2/user/profile/basic`, upserts into
  `whoop_tokens`, sends a **"✅ WHOOP reconnected" Telegram confirm**, and returns an HTML success page.
- **Reconnect UX (v3.18.0):** `whoop-webhook` DMs a debounced "⚠️ reconnect" button when the refresh
  chain dies (`getValidAccessToken` → "re-auth needed"); `telegram-webhook` replies to `/whoop` with
  the same button on demand. The button URL is `…/whoop-oauth?t=<ticket>`. So a dead token → tap →
  WHOOP login → done, no CLI (and it works for any profile, e.g. Dea via her own login). The
  **redirect URI `…/functions/v1/whoop-oauth` must be registered in the WHOOP developer dashboard.**
- **Additional secrets:** `WHOOP_CLIENT_ID`, `WHOOP_CLIENT_SECRET`, `TELEGRAM_BOT_TOKEN` (confirm DM).

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
| **Dispatch targets** | `inbox-drain` (from `trigger-drain`) — `POST /repos/Pilot1940/healthspan-foundation/dispatches`. (`send-brief` dispatch from `telegram-webhook` was removed 2026-06-08; `send-brief.yml` now runs via `workflow_dispatch` only.) |

**Secrets required in GitHub Actions:**

| Secret | Used by |
|---|---|
| `SUPABASE_URL` | inbox-drain, send-brief, ci (integration) |
| `SUPABASE_ANON_KEY` | inbox-drain, send-brief, ci (integration) |
| `SUPABASE_SERVICE_ROLE_KEY` | inbox-drain, send-brief |
| `DATABASE_URL` | inbox-drain, send-brief, ci (integration; tests self-skip when absent) |
| `HS_AUTH_EMAIL` | inbox-drain, send-brief, ci — `healthspan.drainer@chitalkar.com` |
| `HS_AUTH_PASSWORD` | inbox-drain, send-brief, ci (integration) |
| `HS_ANTHROPIC_API_KEY` | inbox-drain, send-brief (passed as `ANTHROPIC_API_KEY` in send-brief) |
| `TELEGRAM_BOT_TOKEN` | inbox-drain, send-brief |
| `WHOOP_CLIENT_ID` | inbox-drain, send-brief — `refresh_recent` WHOOP token refresh in CI |
| `WHOOP_CLIENT_SECRET` | inbox-drain, send-brief — `refresh_recent` WHOOP token refresh in CI |

**Workflows:**

- **`inbox-drain.yml`** — Trigger: `repository_dispatch: inbox-drain` (from
  `trigger-drain`), OR daily cron **03:30 UTC / 09:00 IST** (backstop), OR manual
  `workflow_dispatch`. On dispatch:
  `python3 -m monitor.inbox_drain --once --settle-sec 5` (short settle batches album
  bursts); otherwise `--once` (no settle). **Timeout: 15 min.** Reads `pending`
  `media_inbox` rows, fetches media from `health-media`, calls Claude to extract, writes to
  staging (or production via the `maintainer_ingest_*` RPC if complete), sends a Telegram
  confirmation. A `concurrency` group (`cancel-in-progress: false`) serialises drains —
  concurrent dispatches queue, never drop (commit `bc672c9`).
- **`send-brief.yml`** — Trigger: manual `workflow_dispatch` with optional `profile_id`
  (the `repository_dispatch: send-brief` hook remains in the file but nothing fires it —
  the telegram-webhook text dispatch was removed 2026-06-08; brief requests via Telegram
  are composed inline by the drain). `workflow_dispatch` with a profile_id runs
  `python3 -m monitor.brief --profile-id <uuid>`; without one runs `--all`.
  **Timeout: 10 min.**
- **`media-retention.yml`** — Trigger: daily cron **22:00 UTC / 05:00 ICT**, OR manual
  `workflow_dispatch` (with a `dry_run` input). Runs `python3 -m monitor.media_retention`
  (stdlib-only, service_role): deletes `health-media` Storage objects whose `media_inbox`
  row is terminal (`done`/`failed`) and older than `media.retention_days` (system_config,
  seeded 45 by mig 062), then nulls `storage_path` — the DB row is kept for audit.
  Pending/processing/staged rows are never touched. Exits non-zero on any hard failure.
  **Timeout: 10 min.**
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
| `GH_DISPATCH_TOKEN` | active | trigger-drain (inbox-drain dispatch). No longer used by telegram-webhook — the text → send-brief dispatch path was removed 2026-06-08. |
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
  - **Storage RLS (mig 053, 2026-06-09):** `storage.objects` has RLS **enabled**; the only policy is
    `drainer_read_health_media` — `SELECT` for the drainer service account
    (`healthspan.drainer@chitalkar.com`) on `bucket_id = 'health-media'`. The telegram-webhook
    **uploads with the service-role key** (bypasses RLS). The drain reads as the **authenticated**
    drainer user, so it needs this policy — without it `get_signed_url()` returns NULL,
    `image_blocks` is empty, and the vision model never sees the pixels (the bug that made caption-less
    photos "brief" and captioned photos read from caption text only). End-users have **no** direct
    media read access yet — only the drainer is granted (broaden here if a user-facing media viewer is
    ever built).
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

### 2.6 Claude models & token cost

Model ids are centralized in `lib/models.py` (`HAIKU` = `claude-haiku-4-5`, `SONNET` =
`claude-sonnet-4-6`); call sites read `system_config` first then fall back. `models.create_message()`
turns a retired/invalid id into a clear error (the old hardcoded `claude-3-5-haiku-20241022` in
`food.py` would have 404'd — retired 2026-02-19).

| Path | Model | $/1M (in/out) | ~tokens/call | Frequency |
|------|-------|---------------|--------------|-----------|
| Vision extract (photo) | Sonnet 4.6 | $3 / $15 | ~2,460 in · 400 out (image ≈1,600) | per photo — **the cost driver** |
| Vision extract (text) | Sonnet 4.6 | $3 / $15 | ~860 in · 300 out | per text log |
| describe_stage (clarify msg) | Sonnet 4.6 | $3 / $15 | ~400 in · 150 out | per staged item |
| content clustering | Sonnet 4.6 | $3 / $15 | ~300 in · 100 out | per chat w/ ≥2 ungrouped |
| daily brief actions | Sonnet 4.6 | $3 / $15 | ~600 in · 150 out | per brief (dedup'd) |
| food.py classify / WHOOP screenshot | Sonnet 4.6 | $3 / $15 | ~1k / ~4k in | rare / manual |

> **Estimated Anthropic API cost ≈ $6–12/month** (point estimate **~$8**) for **2 active adults**
> logging ~25 items/day (~half photos). **one model everywhere = Sonnet 4.6** (2026-06-09, commit `d4e2590`) — the brief/clustering/food calls are now Sonnet too (+~$0.50/mo vs the old Haiku split).
> **~1.3¢ per photo.** Swing factor is photo volume, not model choice. 3 active users (Nanki) ≈
> $10–15/mo. Infra (Supabase / GH Actions / AWS) is separate, mostly free-tier. App cost — not the
> Claude Code dev session (`/cost`).

> **One model (Sonnet) by policy** — `lib/models.py` keeps `HAIKU` available for a per-path
> `system_config` override (e.g. `brief.model = claude-haiku-4-5`) if cost ever matters, but nothing
> uses it by default. Caching (#3/#5) is not worth it: vision prompt (~730 tok) is below Sonnet's
> 2,048-token cache minimum, and image caching costs +25% on the common single-vision path.

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
  │  4. per-item claim (claim_inbox_item CAS, pending→processing) with compensation —
  │     lose any row of a cluster → release the claimed ones back to pending
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

#### Cluster claim (per-item CAS + compensation)

Before any extraction, `monitor/inbox_drain.py` claims each row of a cluster via
`DbRest.claim_inbox_item` — a filtered PATCH (`status=eq.pending` → `processing`)
so only one drain wins a given row. If **any** row of the cluster was already taken
by a concurrent drain, the rows this run did claim are released back to `pending`
and the cluster is skipped — all-or-nothing semantics by compensation, not by lock.
This prevents two overlapping workflow runs (e.g. event-path + cron-path firing
close together) from double-writing the same album. (The `claim_inbox_cluster(uuid[])`
RPC from migration 046 was never actually wired in and was dropped in migration 061.)

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

**Food→supplement bridge (mig 069, BACKLOG #27).** Food and supplements are separate
tables/write paths and the drain classifies a message as ONE kind, so a supplement named
inside a food log ("shake with creatine") never logged as an intake. After a food row
inserts, `bridge_food_supplements()` scans the food text (description + `foods[].name` +
caption) for the user's **own active regimen** supplements only (never the catalog, never
learns — glutamine isn't in the regimen → ignored; creatine → logged). Matching
(`match_regimen_supplements_in_text`) is whole-PHRASE/whole-token: a bare "magnesium"
matches NEITHER magnesium variant; "nac" ≠ "snack". It's idempotent — skips items already
logged that day and pins `taken_at` to **noon-UTC** so the `UNIQUE (profile_id,
supplement_id, taken_on, source='telegram')` ON CONFLICT collapses bridge + 📝-menu +
"took creatine" text into one row. The confirmation appends `💊 Also logged: …` (minor-safe,
runs for all). Failure-isolated (after the food commits, try/except → `summary["errors"]`).
Gated by `food_supplement_bridge.enabled` (default true). NOT wired to the reply-supersede
loop (same limitation supplements have generally — mig 054).

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
| **Concurrent drains on same album** | Per-item `claim_inbox_item` CAS lets only one win each row; a partial loser releases its claimed rows back to `pending` and skips the cluster. | The winner processes; no double-write. |

#### Minor-safety framing

The `is_minor` flag on `telegram_identities` changes both the ack text (step 9) and any
push/confirmation wording (`compose_confirmation`, `composePush`): growth/performance
language only, never deficit/restriction words. This is enforced at the
`telegram-webhook` ack and again in the Python drain.

### 3.2 Text Message → Log (or Daily Brief) Pipeline

**LLM-routed text (2026-06-08): the regex gate is GONE.** ALL text-only Telegram messages
enqueue to `media_inbox` (`kind='unknown'`, `storage_path=null`) and flow through the
*same* drain as photos. The drain's unknown-classifier LLM decides per message: a **LOG**
(food / supplement / biomarker — multi-item, e.g. "i took D3, K2, B12, magnesium citrate,
omega-3" auto-logs all five, doses defaulted from the active regimen) or a **brief
request** (`{kind:"brief"}` — e.g. "how am I doing today?"), which the drain composes
inline at end-of-run via `compose_brief()`. There is no `parseLogCommand` and no
`send-brief` dispatch from the webhook any more; `guessKind` survives only as a
photo-caption hint.

```
USER (Telegram text)
  ▼
telegram-webhook
  │  Identity + update_id dedup as above
  │  INSERT media_inbox { kind:'unknown', storage_path:null, caption:text }
  ▼
pg_net trigger → trigger-drain → repository_dispatch: inbox-drain
  ▼
monitor/inbox_drain.py (unknown-classifier LLM, Sonnet)
  ├─ log items   → maintainer_ingest_* RPCs → "✅ … logged"
  └─ {kind:"brief"} → compose_brief() inline → Telegram reply (the daily brief)
```

An explicit brief request always sends — only AUTO post-log briefs dedup across runs via
`push_log` + `brief.dedup_sec` (commit `d2d5d3d`).

**Clarify auto-match (BACKLOG #15, 2026-06-10).** Users — especially a 14-year-old — answer
the bot's "reply to fix it" question with a *fresh message*, not a Telegram reply. Two
drain-side layers absorb that habit: **Layer 1** (`absorb_pending_clarify`) — a fresh text
message arriving within `clarify.match_window_sec` (900s) of a pending clarify for the same
chat is LLM-judged as a candidate answer; at ≥ `clarify.match_min_conf` (0.7) it is
processed AS the clarification (original caption + image) and the stranded staged item
retires. On any doubt it processes independently. **Layer 2** (`sweep_staged_orphans`,
end-of-run) — a staged item whose profile logged a matching prod row within
`clarify.orphan_supersede_window_sec` (1800s) retires as superseded (`result_ref` → the
prod row). The Telegram-reply path is unchanged and remains the highest-confidence signal.

`send-brief.yml` remains for cron/manual briefs via `workflow_dispatch`: with a
`profile_id` input it briefs that one profile; without one it runs `monitor.brief --all`
for every profile.
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
  │       workout → /v2/activity/workout/{id} · sleep → /v2/activity/sleep/{id}
  │       recovery → /v2/recovery collection keyed sleep_id (the event id is the
  │                  SLEEP UUID) → /v2/cycle/{cycle_id}
  │       cycle (never emitted) → /v2/cycle/{id} + /v2/cycle/{id}/recovery
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
error), renders minor-safe text, calls Claude (Sonnet — default `models.SONNET` =
`claude-sonnet-4-6` from `lib/models.py`, overridable via `system_config` key
`brief.model`) for the rest-of-day actions, sends over Telegram, and returns the message
text.

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
| **Rest-of-day actions** | 2–4 Claude-generated (Sonnet) next actions | Empty string on Claude failure (best-effort, never aborts the brief) |

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
  → per-item claim_inbox_item CAS → Claude extract → maintainer_ingest_food RPC
  → food_logs (complete) OR stg_food_log_review (incomplete)
  → Telegram confirmation

Telegram text  → telegram-webhook → media_inbox (kind=unknown) → same drain path
                 (LLM routes log-vs-brief; brief requests compose inline at end-of-run)
```

A parallel, longer-lived fallback fires from `telegram-webhook` itself (`maybeFireRoutine`,
debounced 300s via `system_config.routine.last_fire_at`) to `ROUTINE_TRIGGER_URL` if
configured. The pg_net path (15s dedup) is the primary real-time path.

---

## 4. DATABASE SCHEMA

Supabase Postgres with Row Level Security on every table. The applied DB state is the
source of truth — there is no `schema_migrations` table; the migration filename sequence
(`001`…`NNN`) is the canonical ordering record.

### 4.1 Tables (47 active / 16 dormant of 63 base + 7 views, 2026-06-11)

> Live DB as of 2026-06-11: **63 base tables + 7 views** — **47 active / 16 dormant** (dormant = zero live rows per `pg_stat_user_tables` estimates: `audit_log, biomarker_targets, body_metrics_history, canonical_aliases, daily_log_metrics, daily_logs, documents, food_rules, ingestion_tokens, program_workouts, stg_biomarker_review, stg_food_rule_review, stg_test_result_review, test_targets, trend_alerts, weight_logs`). `strength_logs` (mig 063) + `whoop_oauth_codes` (mig 065) are the newest base tables. Figures are point-in-time — re-count via `select count(*) from information_schema.tables where table_schema='public' and table_type='BASE TABLE'`.


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
| whoop_oauth_codes | Mig 065. One-time, expiring tickets for the Telegram WHOOP-reconnect flow: `code (PK), profile_id, created_at, expires_at, used_at`. The ticket (not the raw profile_id) is the OAuth `state` in the reconnect URL; `whoop-oauth` consumes it single-use on the WHOOP callback. RLS maintainer-only; service_role for the edge functions. |
| telegram_processed_updates | Inbound idempotency latch: `update_id (PK), received_at`. Written AFTER the `media_inbox` insert succeeds, never before. |
| whoop_tokens | WHOOP OAuth credentials: `profile_id, access_token, refresh_token, expires_at, scope, whoop_user_id`. service_role only — RLS default-deny for all other roles. Never surfaced in skill output. |
| families | Family container (1 row — Chitalkar): `id, name, created_by`. Root of `family_memberships`. |
| users_extended | Legacy v1 per-auth-user profile record (`display_name, date_of_birth, sex, blood_type, timezone, unit_system, …`). Superseded by `profiles`; 1 row retained. |

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
| supplement_aliases | Name-matching aliases: `alias → supplement_id` (25 rows). Used when resolving free-text supplement names. |
| supplement_components | Compound products: `product_id → component_id, amount, unit` (e.g. a multi maps to its actives). |

#### Biomarkers

| Table | Purpose / key columns |
|---|---|
| biomarkers | `profile_id, metric_definition_id, value, qualifier, unit, source, document_id, staging_id, measured_at, lab_name, healthspan_test_id, notes`. `qualifier` (`<`/`>`/`<=`/`>=`/`~`) preserves detection bounds vs exact values. HOMA-IR/eGFR are lab-printed derived values — never recompute. UNIQUE (profile_id, metric_definition_id, measured_at). |
| metric_definitions | Marker catalog: `name, display_name, category, data_type, unit, loinc_id, min_value, max_value, plausible_min, plausible_max, decimal_places, is_active`. `min/max_value` = clinical reference range; `plausible_min/max` = wider physiological gate used by `lib/contract` to route OCR/unit errors to staging. |
| healthspan_tests | Lab test events: `profile_id, test_definition_id, tested_at, lab_name, provider, investigation_type, status`. Biomarker rows link back via `healthspan_test_id`. |
| test_definitions | Panel catalog: `name, display_name, category, biomarker_count` (e.g. full blood panel definitions). |
| loinc_reference | LOINC code catalog (55 rows): `loinc_code, component, long_name, unit, …`. Lookup support for `metric_definitions.loinc_id`. |

#### WHOOP Wearables

| Table | Purpose / key columns |
|---|---|
| **whoop_cycles** | `id, profile_id, cycle_start, cycle_end, timezone, recovery_score_pct, resting_hr_bpm, hrv_ms, skin_temp_celsius, blood_oxygen_pct, respiratory_rate_rpm, day_strain, energy_burned_cal, max_hr_bpm, avg_hr_bpm, sleep_onset, wake_onset, sleep_performance_pct, asleep_duration_min, in_bed_duration_min, light_sleep_min, deep_sws_min, rem_min, awake_min, sleep_need_min, sleep_debt_min, sleep_efficiency_pct, sleep_consistency_pct, sprint_id, source_file, whoop_id`. **Mig 045 added:** `score_state, recovery_score_state, recovery_user_calibrating, sleep_cycle_count, disturbance_count, no_data_min, whoop_updated_at, whoop_created_at`. Traps: NULL recovery/HRV = no sleep that cycle (never 0); `cycle_start` is the WHOOP day boundary (screenshot date minus 1 day) — join to journal on `::date`, not exact timestamp. |
| whoop_sleeps | Per-sleep record. `is_nap = TRUE` rows separate from main sleep (most analyses filter `is_nap = false`). Mig 045 added `score_state, no_data_min, sleep_cycle_count, disturbance_count, whoop_cycle_id, whoop_updated_at, whoop_created_at`. |
| whoop_workouts | Per-workout: zone %/seconds, strain, HR, loads, `tags`/`protocol` (jsonb), `whoop_id`. Mig 045 added `score_state, sport_id, percent_recorded, distance_m, altitude_gain_m, altitude_change_m, whoop_updated_at, whoop_created_at`. |
| whoop_body_measurements | Mig 045: `profile_id, synced_at, synced_date, height_m, weight_kg, max_heart_rate`. UNIQUE (profile_id, synced_date). |
| whoop_journal_entries | Long-format behaviour log: `profile_id, cycle_start, behavior_id, answered_yes, quantity, unit, notes, source_file`. UNIQUE (profile_id, cycle_start, behavior_id). Join to cycles on `cycle_start::date`. |
| journal_behaviors | WHOOP journal behaviour catalog (27 rows): `slug, question_text, display_name, category, is_quantitative, default_unit`. |
| journal_behavior_aliases | Free-text → `behavior_id` aliases (28 rows) for journal ingestion. |
| hr_zone_config | Per-profile HR zone boundaries: `profile_id, effective_date, max_hr_bpm, z0–z5 bounds`. Used for workout zone analysis. |
| workout_hr_samples | Raw HR time-series per workout: `workout_id, t_offset_sec, hr_bpm`. |
| workout_intervals | Per-interval workout detail: `workout_id, interval_index, kind, duration, peak/avg/min HR, recovery drop`. |

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
| **strength_logs** | Mig 063. Resistance-training sets: `profile_id, performed_at, performed_on (GENERATED date, UTC), exercise, modality (barbell\|machine\|cable\|dumbbell\|bodyweight), load_value, load_unit (kg\|lb\|bodyweight — never 'plates'), sets, reps, rir, device_specific, notes, source, voided_at, void_reason`. Append-only; void via `maintainer_void_strength()`. RLS + grants mirror `food_logs` (FOR ALL on `has_profile_access`; authenticated S/I/U, no DELETE). Every read must carry `WHERE voided_at IS NULL`. |
| user_goals | `profile_id, metric_definition_id, title, target_value/unit/date, is_active, progress_pct, program_id, baseline_value, achieved_value, achieved_date`. Multiple concurrent active goals allowed (mig 020). |
| sprints | `profile_id, name, slug, location, start_date, end_date, goals (jsonb)`. No status column — derive active/completed/upcoming from dates vs today. **`goals` is an OBJECT** (v2): `{block_goals:[str], weekly_plan:{<weekday>:{sessions:[str],intensity,hard?,recovery?}}, rules:[str], adherence_log:{<YYYY-MM-DD>:{gym,beach,pool,hike,massage}}, daily_overrides:{<YYYY-MM-DD>:{sessions[],intensity,hard?,recovery?}}}`. `daily_overrides[today]` SUPERSEDES `weekly_plan[weekday]` for that one date. Legacy rows are still a flat `[str]` — read via `lib/sprints.normalize_goals()`. The daily brief renders today's plan + adherence from it; the `sprints_status` view returns `block_goals` for object rows so existing consumers keep the goal-strings contract. |

#### Monitoring

| Table | Purpose / key columns |
|---|---|
| wearable_sync_log | `profile_id, provider, sync_type, status (CHECK pending\|in_progress\|success\|failed), method (CHECK csv\|screenshot\|photo\|manual\|api), records_in/upserted/skipped/failed, document_id, source_path, started_at, completed_at`. Maintainer-only SELECT. |
| wearable_sync_errors | `sync_log_id, record_ref, error_code, error_message, raw (jsonb)`. Maintainer-only SELECT. |
| query_audit | `profile_id, ts, kind (CHECK catalog\|adhoc), name_or_sql, params (jsonb), quality_verdict (jsonb), row_count, flagged`. Every skill read lands here. Maintainer-only SELECT. |
| trend_alerts | `profile_id, metric_definition_id, alert_type, severity, title, message, data (jsonb), is_read`. `(empty)` — alerting not yet wired. |
| push_log | Push/brief ledger: `profile_id, push_type (brief\|recovery_landed\|workout_logged\|…), subject_id, sent_at, status (sent\|suppressed), dedup_value`. Written by whoop-webhook Phase-2 pushes AND the drain's cross-run brief dedup (`brief.dedup_sec`). |

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
| `maintainer_ingest_supplement` | DEFINER | 12-param. Routes to `supplement_intake_logs` or `stg_supplement_intake_review`. Stages on `p_force_stage` or `supplement_id IS NULL`. `taken_on` removed from INSERT (GENERATED, 428C9 fix mig 039); `source` CHECK includes `telegram` (mig 040). ON CONFLICT re-log **clears `voided_at`** (mig 060 — a voided intake that is re-logged comes back). Returns `v_stage_reason`. |
| `maintainer_ingest_biomarker` | DEFINER | 12-param. Routes to `biomarkers` or `stg_biomarker_review`. Stages on `p_force_stage`, `metric_definition_id IS NULL`, `value IS NULL`, or value outside `plausible_min/max`. ON CONFLICT re-ingest **clears `voided_at`** (mig 060). |
| `maintainer_void_supplement(p_id, p_reason)` | DEFINER | Mig 060 (BACKLOG #18). Soft-deletes a `supplement_intake_logs` row (`voided_at = now()`, `void_reason`); maintainer + profile-access gated, reason ≥5 chars, idempotent (`already_voided`). Voided rows are excluded by every read path (`WHERE voided_at IS NULL`). The scoped replacement for the revoked blanket DELETE grant. |
| `maintainer_void_food(p_id, p_reason)` | DEFINER | Mig 060 — same contract, for `food_logs`. |
| `maintainer_void_biomarker(p_id, p_reason)` | DEFINER | Mig 060 — same contract, for `biomarkers`. |
| `maintainer_void_strength(p_id, p_reason)` | DEFINER | Mig 063 — same contract, for `strength_logs`. |
| `lookup_food_reference(p_name, p_profile_id)` | DEFINER | Best macro match from `food_reference`; personal rows beat global. |
| `lookup_viome_verdicts(p_items[], p_profile_id)` | DEFINER | Returns avoid/minimize/superfood verdicts for a list of ingredient names. |
| `promote_food_to_reference(p_name, p_profile_id, …)` | DEFINER | Upserts a user-confirmed food into their personal `food_reference` library. |
| `claim_inbox_item(item_id)` | client method (`lib/db_rest.py`) | Single-item claim via filtered PATCH (`status pending → processing`, `prefer_return`); returns `True` if this caller won the race. Not a DB RPC — runs under the drainer's JWT, governed by `media_inbox` RLS. |
| `fn_media_inbox_notify()` **(trigger)** | DEFINER | `AFTER INSERT` on `media_inbox` (`trg_media_inbox_notify`); fires `net.http_post` to the `trigger-drain` Edge function for each new `pending` row. Reverted to anon-key-from-`system_config` header in mig 047 (vault lookup returned NULL in pg_net context). |
| `is_maintainer(p_uid DEFAULT NULL)` | DEFINER | Resolves the maintainer flag via `family_memberships JOIN profiles` (mig 023) so any of PC's auth identities is recognised. |
| `has_profile_access(uuid)` | DEFINER | True if the calling auth identity has a `family_memberships` row for the target profile. The backbone of every profile-scoped RLS policy. |
| `learn_supplement(p_profile_id, p_name)` | DEFINER | Mig 052. Adds a user-named supplement to the catalog (dedups by normalised name; stamps `source='learned'`, `verified=false`). Adults only — called by the drain's learn-on-clarify path. |
| `sprint_set_adherence(sprint, date, activity, value, profile)` | DEFINER | Mig 066; mig 068 widened `activity` to `gym\|beach\|pool\|hike\|massage\|iron\|calcium\|vitamin_d` (nutrition keys). Atomic, subtree-scoped write of `goals.adherence_log[date][activity]` (single `jsonb_set`). The TRACKING-surface write contract — Telegram `applyTick` + `lib/sprints.mark_done`. Ownership-gated (authenticated needs `has_profile_access`; service_role trusted, sprint pinned to `profile`). Returns the day's adherence map. ⚠️ Assumes **object** `goals` — RAISEs on a legacy flat-ARRAY `goals` row (pre-v3.17); only inactive old sprints are arrays, so live ticks (always the active sprint) are unaffected (BACKLOG #29). |
| `sprint_set_override(sprint, date, override, profile)` | DEFINER | Mig 066. Atomic, subtree-scoped write of `goals.daily_overrides[date]` (`NULL`/`{}` clears via minus-key). The PLANNING-surface write contract — the claude.ai skill + `lib/sprints.set_override`. Same ownership gate. Can't clobber a concurrent adherence tick (different subtree, server-side merge). |
| `hs_close_sync_log(...)` | DEFINER | Mig 030. Closes a `wearable_sync_log` row (status + counters) from the drain without a maintainer session. |
| `ingest_health_artifact(...)` | DEFINER | Mig 002 **legacy** token-gated ingestion entry, superseded by the Telegram path. EXECUTE **revoked from anon + authenticated** in mig 061 (was the Postgres default PUBLIC grant); function kept, callable only as postgres/service_role. |
| `mint_ingestion_token(...)` | DEFINER | Mig 002 legacy — mints `ingestion_tokens` rows. EXECUTE revoked from anon + authenticated (mig 061); `hs_ops.py mint-token` runs as postgres and still works. |
| `whoop_journal_insert_through()` **(trigger)** | DEFINER | Mig 007. INSTEAD OF INSERT write-through on the `whoop_journal` pivot view → `whoop_journal_entries`. |
| `rls_auto_enable()` **(event trigger)** | DEFINER | Event trigger `ensure_rls` (ddl_command_end): auto-enables RLS on any new `public` table. Created out-of-band; versioned into the repo by mig 058 (2026-06-10). |

### 4.3 RLS Model

- **RLS is enabled on every public table.** New tables without an RLS policy in the same migration do not pass review (Rule 3).
- **Two PostgREST roles:**
  - `authenticated` — every skill/drain session. All reads and writes are scoped to the caller's accessible profiles via `has_profile_access()` / `is_maintainer()` policies. Cannot see other families' data.
  - `service_role` — bypasses RLS entirely. Used **only** by Edge functions and sync jobs (e.g. `whoop_tokens` access, webhook inserts). Never used by the skill.
- **`healthspan_app`** — a Postgres LOGIN role for the direct-psycopg2 path (PC local). Member of `authenticated` with `NOINHERIT`, so it must `SET ROLE authenticated` and set JWT claims explicitly; a bare connection sees nothing. Inherits mig 010's grants and all RLS via that path. DELETE/TRUNCATE revoked directly.
- **Maintainer-only tables** (SELECT gated to `is_maintainer()`): `query_audit`, `wearable_sync_log`, `wearable_sync_errors`, `stg_food_log_review`, `stg_supplement_intake_review`, `stg_biomarker_review` (+ `stg_food_rule_review`, `stg_test_result_review`). Dea/Dev sessions see zero rows on these by design — they see outcomes, never the machinery. INSERTs to these tables use client-generated UUIDs and no `RETURNING` (which would invoke the SELECT policy). (Mig 022 originally missed three of these — `stg_supplement_intake_review`, `stg_food_rule_review`, `stg_test_result_review` kept their mig-004 `ALL/has_profile_access` policy, so a non-maintainer could SELECT/UPDATE her own staged rows. **Fixed by mig 058, applied 2026-06-10** — all eight now follow the maintainer-SELECT + owner-INSERT pattern, verified live. **Mig 059** added maintainer-gated UPDATE policies on the five `stg_*_review` tables so the drain — which passes `is_maintainer()` via its membership to PC's maintainer profile, the same mechanism that authorises its `maintainer_ingest_*` calls — can retire review rows for the clarify auto-match; non-maintainers still cannot UPDATE.)
- **Drain service account:** `healthspan.drainer@chitalkar.com` (env `HS_AUTH_EMAIL`). Holds `owner` `family_memberships` to PC, Dea, and Dev profiles so the three `maintainer_ingest_*` RPCs pass `has_profile_access()`. UID resolved dynamically (mig 035) — no hardcoded UUID. Not itself a maintainer.
- **Grant hardening** (mig 010/014): DELETE and TRUNCATE revoked from `authenticated` and `healthspan_app` on all public tables; `ALL` revoked from `anon`; shared catalogs are SELECT-only for `authenticated`.

### 4.4 Migration History (001–071)

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
| 050 | food_kcal_min_zero | Food kcal plausibility floor lowered 25→0 (zero-calorie items: black coffee, water). |
| 051 | supplements_learned_stamps | `supplements.source`/`verified` stamps for the learn-on-clarify path (+ partial index sorting on the pre-existing `created_at`, from mig 004). |
| 052 | learn_supplement_rpc | `learn_supplement` RPC — auto-adds a user-named supplement not in the catalog (dedup by normalized name; `source='learned'`, `verified=false`). Adults only. |
| 053 | storage_drainer_read | Storage RLS `SELECT` policy granting the drain service account read on the private `health-media` bucket (the real reason photos never reached the vision model). |
| 054 | media_inbox_logged_food_ids | `media_inbox.logged_food_ids` — reply-to-correct supersedes an auto-LOGGED food item (webhook deletes those `food_logs`, re-queues; no double-count). |
| 055 | media_inbox_staged_review_ids | `media_inbox.staged_review_ids` — clarify-supersede retires the staged `stg_food_log_review` row (no phantom review-queue entry). |
| 056 | brief_minor_optin | Seeds `system_config` `brief.minor_optin_profile_ids` (JSON array) — per-profile maintainer-consent allowlist for sending the daily brief to a MINOR (Dea opted in; Dev/future minors off by default). Data-only (no new table). |
| 057 | reticulocyte_count_definition | Adds the `reticulocyte_count` metric_definition (unit %, ref 0.5–2.5, plausible 0–15) so the last Jun 2026 Metropolis marker could ingest. Data-only; applied via the postgres role (authenticated cannot INSERT into `metric_definitions`). |
| 058 | stg_review_rls_parity | **Applied 2026-06-10.** Fixes the three `stg_*_review` tables mig 022 missed (supplement_intake / food_rule / test_result → maintainer-only SELECT + owner INSERT); seeds `brief.dedup_sec=600` (rule-#1 parity); versions the out-of-band `rls_auto_enable` event-trigger function. Verified live: all 6 policies, config row, `ensure_rls` trigger. |
| 059 | clarify_automatch | **Applied 2026-06-10.** BACKLOG #15: seeds `clarify.match_window_sec` (900) / `clarify.match_min_conf` (0.7) / `clarify.orphan_supersede_window_sec` (1800); adds maintainer UPDATE policies on the five `stg_*_review` tables so the drain can retire review rows on clarify auto-match / orphan sweep. |
| 060 | void_soft_delete | **Applied 2026-06-10.** BACKLOG #18: `voided_at`/`void_reason` on `supplement_intake_logs`/`food_logs`/`biomarkers`; `maintainer_void_supplement/_food/_biomarker` SECURITY DEFINER RPCs (maintainer-gated, idempotent, reason ≥5 chars); supplement+biomarker ingest RPCs un-void on ON CONFLICT re-log; `daily_health_summary` food subselects exclude voided; **REVOKE DELETE ON ALL TABLES FROM healthspan_app** (the 2026-06-08 grant had landed on all 60+ tables). Live-verified in a rolled-back txn. |
| 061 | security_hygiene | **Applied 2026-06-10.** BACKLOG #21: `media_inbox`/`push_log` INSERT scoped to `authenticated` + `has_profile_access OR is_maintainer`; open INSERT policies on `telegram_processed_updates`/`wearable_sync_errors` dropped (service_role/postgres writers only); EXECUTE on legacy `ingest_health_artifact`/`mint_ingestion_token` revoked from anon+authenticated; 3 dead config keys deleted; unused `claim_inbox_cluster(uuid[])` dropped. |
| 062 | media_retention_config | **Applied 2026-06-10.** BACKLOG #7: seeds `media.retention_days` (45) for the health-media Storage prune (`monitor/media_retention.py` + `media-retention.yml` daily cron). |
| 063 | strength_logs | **Applied 2026-06-11.** New `strength_logs` table (resistance-training sets: load/sets/reps/RIR + `performed_on` GENERATED UTC date). Soft-delete via `voided_at`/`void_reason` + `maintainer_void_strength()` (mirrors mig 060); RLS + grants copied from `food_logs` (FOR ALL on `has_profile_access`; authenticated S/I/U, no DELETE). Seed is separate (`scripts/seed_strength_063.py`) — transportable schema, no personal data in the migration. All 4 sets seeded (PC confirmed `machine_chest_press` = 140 lb 2026-06-11; was recorded as 'plates'). Verified live. |
| 064 | food_reference_common_items | **Applied 2026-06-11.** BACKLOG #25: brand-accurate aliases for PC's frequent items (`food_reference` matches by EXACT lower(name)/alias). Sets `brand='Proten'` + `proten*` aliases on the Thai Tea row (brand is "Proten", not "protein"); adds the distinct **Hooray Banana** shake (220 kcal/31P, vs Strawberry's 250/different carbs). Idempotent (works on fresh + live). PC's personal "TWT" (The Whole Truth) morning whey+creatine+glutamine shake seeded separately (`scripts/seed_food_ref_personal_064.py`) — profile-scoped, not in the migration. All three resolve via `lookup_food_reference` (verified). |
| 065 | whoop_oauth_codes | **Applied 2026-06-11.** Self-serve WHOOP reconnect from Telegram: `whoop_oauth_codes` one-time, expiring tickets carry the OAuth `state` (the ticket, not the raw `profile_id`, travels in the reconnect URL, so a shared link can't attach a stranger's WHOOP to a profile). RLS maintainer-only (edge functions use service_role). Seeds config `whoop.oauth_ticket_ttl_min` (30) + `whoop.reauth_alert_debounce_hours` (24). Consumed single-use by `whoop-oauth` on the WHOOP callback. |
| 066 | sprint_goals_atomic_writers | **Applied 2026-06-12.** Lost-update fix for `sprints.goals`: two field-scoped, ownership-gated SECURITY DEFINER RPCs — `sprint_set_adherence(sprint,date,activity,value,profile)` (TRACKING — Telegram ticks → `adherence_log`) and `sprint_set_override(sprint,date,override,profile)` (PLANNING — claude.ai skill → `daily_overrides`; NULL/`{}` clears). Each is a single `jsonb_set` merging ONLY its own subtree server-side, so the two write surfaces can no longer clobber each other (the 2026-06-12 'beach' tick revert) and same-subtree writes serialize on the row lock. Gate: authenticated needs `has_profile_access`; service_role trusted but the sprint is pinned to the passed `profile_id`. `lib/sprints.mark_done`/`set_override` + webhook `applyTick` (telegram-webhook v10) all repointed to these. |
| 067 | view_security_invoker_rls_leak | **Applied 2026-06-12.** SECURITY FIX — a Postgres view runs with the VIEW OWNER's privileges and BYPASSES base-table RLS unless `security_invoker=true` (PG15+). `daily_health_summary` (recreated in mig 060 without it) leaked EVERY profile's rows to any authenticated caller (Dea saw PC). Recreated with `security_invoker=true`. Rule: any view over a profile-scoped table MUST set it, and `CREATE OR REPLACE` can silently drop it. |
| 068 | sprint_adherence_nutrition_keys | **Applied 2026-06-12.** Food micronutrient check-in: Dea tracks IRON/CALCIUM/VITAMIN D via FOOD (no pills/regimens over REST), reusing the sprint `adherence_log`. CREATE OR REPLACEs mig 066's `sprint_set_adherence` to also accept `'iron'`/`'calcium'`/`'vitamin_d'` (was hard-validated to the 5 workout activities); everything else byte-for-byte identical. Function-definition change only; brief renders a separate "🥗 Food check-in" line. |
| 069 | food_supplement_bridge_config | **Applied 2026-06-14.** BACKLOG #27: seeds `food_supplement_bridge.enabled` (default true) — the flag for the food→supplement bridge (regimen supplements named inside a food log now also log as intakes; see §3.1 / `monitor/inbox_drain.py:bridge_food_supplements`). Config-only (Rule #1, no hardcoded toggle); idempotent ON CONFLICT upsert. |
| 070 | threshold_config_keys | **Applied 2026-06-14.** Deep-scan Rule-#1 cleanup: seeds `brief.food_intake_threshold_pct` (0.6, the minor "keep fuelling" floor used by brief.py + inbox_drain.py), `brief.supp_slot_{morning,lunch,dinner,bedtime}_utc` (5/11/15/20, brief supplement-slot hour boundaries), and `supplement.adherence_threshold_pct` (70, supplement_summary low-adherence cutoff). Code reads each with an equal code-side fallback, so behaviour is unchanged at defaults. Config-only, idempotent. |
| 071 | proten_thai_tea_aliases | **Applied 2026-06-14.** Appends missing aliases (`proten thai tea protein shake` + the `protien` misspelling, etc.) to the global "Protein Thai Tea Shake" (Proten, 190 kcal/35g) `food_reference` row so PC's phrasing auto-resolves instead of asking for calories. `lookup_food_reference` matches aliases exactly; DISTINCT-merge keeps it idempotent. (Deeper fix = fuzzy match — BACKLOG #25.) |

---

## 5. PYTHON CODEBASE

### 5.1 Module Map

One paragraph per module: what it does, key public functions, and when to reach for it. `lib/contract.py` is the central hub — every ingest source funnels through its `resolve → validate → write → log` pipeline; `lib/db.py` supplies the connection and `lib/views.py` + `lib/sql_guard.py` own the read side.

#### lib/

**db.py** — DB connection factory and profile resolution; reads the DSN from a shared `.db-config.json` (two Dropbox paths or `$HEALTHSPAN_DB_CONFIG`) and never logs it. `get_conn(readonly=False)` opens a raw psycopg2 connection for CLI tools; `resolve_profile(conn, who)` maps a display name ("PC") or UUID to a profile UUID (raises `LookupError` on miss/ambiguity, defaults to "PC"); `get_app_connection(config)` returns either a psycopg2 connection with `SET ROLE authenticated` + JWT claim (`direct_role` mode) or a fully signed-in supabase client (`supabase_client` mode) — never an anon client. **`direct_role.privileged=true`** (v3.16.0) skips the `SET ROLE` so the connection keeps its `postgres` role (RLS bypassed, DELETE/DDL allowed) — the unrestricted maintainer bundle only; the JWT claim is still set so `is_maintainer()`/`has_profile_access()` resolve. `get_app_db_rest(config)` returns a `lib.db_rest.DbRest` carrying the person's JWT (supabase_client mode) for the self-write path. Use it as the entry point for any direct-DB tool; psycopg2 is lazily imported so the supabase path has no psycopg2 dependency.

**db_rest.py** — httpx-based PostgREST client for cloud environments where native packages are unavailable (GH Actions, Claude Routines — no psycopg2). `sign_in(url, anon_key, email, password)` does a password-grant and returns a JWT; `DbRest(url, anon_key, jwt)` is a context manager with `.select/.insert/.update/.rpc` plus `.claim_inbox_item(item_id)`, a compare-and-swap that only flips rows where `status='eq.pending'` so concurrent Routine fires can't double-process. Reach for this whenever code runs in the cloud REST path rather than against a direct connection.

**contract.py** — the shared four-step ingestion contract (Resolve → Validate → Write → Log) that every data source obeys; the central correctness/safety layer. Key functions: `parse_number`, `resolve(conn, kind, raw)` (catalog FK + confidence), `validate` (two-tier: clinical `min/max` flags but never blocks, physiological `plausible_min/max` gates to staging), `write(conn, table, row, conflict_cols)` (idempotent upsert that auto-excludes GENERATED columns from INSERT/UPDATE while keeping them as ON CONFLICT targets), `stage`, `open/close_sync_log`/`log_error`, and `ingest_record(...)` which runs one record through the full gate order under a SAVEPOINT (implausibility → unverified-unattended → low-confidence → hard-validation → prod write). Use it from any new ingest source; thresholds come from `system_config` (never hardcode), and `out_of_range` writes to prod while `implausible` routes to staging.

**context.py** — loads and parses per-person `context/<slug>.context.md` into a structured dict; the profile context is the sole source of targets, norms, coaching voice, and safety constraints (no population defaults). `parse_context_md(text)` is a pure parser; `load_context(profile, base_dir)` reads the file and hard-stops (`FileNotFoundError`) if absent; `get_context(config, base_dir)` bootstraps a session and hard-errors on a profile_id mismatch between context file and config (prevents coaching one person with another's rules); `get_target(ctx, key)` returns a value or None ("not specified — ask, don't assume"). Call it at session start before any targeted coaching.

**views.py** — centralised catalog of all named reporting queries; every emitter (reports, dashboard, exports) must go through it so SQL is never duplicated. `list_views()` returns catalog metadata; `run_view(conn, name, profile_id, **params)` runs an RLS-scoped named view and returns `{name, columns, rows, chart_hint, description, summary?}`, logging each run to `query_audit` inside a savepoint (a read-only connection or RLS failure never breaks the read path). Named views include `v_recovery_30d`, `v_sleep_debt_30d`, `v_workout_zone_summary`, `v_supplement_onoff`, `v_india_vs_travel`, `v_biomarker_timeline`, `abnormal_labs`, `vo2_status`, `hr_drop_compare`, `sprints_status`. NULL recovery (no sleep that cycle) is filtered, never averaged as 0. Use it for any catalog read.

**sprints.py** (v3.19.2) — training-sprint plan + adherence from `sprints.goals` jsonb (pure logic except the two DB writers). `normalize_goals(goals)` reads both the v2 object and the legacy flat array; `weekday_name`, `todays_plan(goals, weekday, today_iso)` (a `daily_overrides[today_iso]` supersedes the `weekly_plan[weekday]` template for that one date), `autoreg(recovery_pct, green_min, yellow_min)` (WHOOP bands, defaults 67/34, overridable), and `render_training_section(sprint, today_iso, recovery_pct, …)` build the brief's 🏋️ Training block (today's sessions + intensity, hard/recovery flag, autoregulated directive, adherence ticks). **Two DB writers, both atomic via mig 066 RPCs (NO full-object read-modify-write):** `mark_done(db, sprint_id, date, activity, value=True, *, profile_id=None)` → `sprint_set_adherence` (a Telegram tick into `goals.adherence_log`); `set_override(db, sprint_id, date, override, *, profile_id=None)` → `sprint_set_override` (the PLANNING skill's write into `goals.daily_overrides`; `None`/`{}` clears). Each RPC `jsonb_set`-merges ONLY its own subtree server-side, so a tick and an override write on the same row can't clobber each other (fixes the 2026-06-12 lost-tick). `profile_id` is the RPC's ownership pin, resolved from the sprint when omitted. Driver-agnostic (DbRest `.rpc` or psycopg2 `SELECT`). `update_button(today)` (v3.19.0) is the single "📝 Update today" button the brief attaches; the two-level menu (training toggles + supplement slots → pills) is rendered server-side in `telegram-webhook` on tap, and its `applyTick` also calls `sprint_set_adherence`. `adherence_keyboard` is retained (documents the layout the Deno side mirrors). The supplement menu (webhook v11, BACKLOG #26 A+C) names the item + slot count in the toggle toast, hints on slot drill-in, and pins inserted `taken_at` to noon-UTC of the menu date so `taken_on` lands on the local day. ⚠️ **Still deferred (#26 B):** a toggle re-renders only the keyboard (`editMessageReplyMarkup`), not the brief's message text.

**sql_guard.py** — two-layer safety on the read path for ad-hoc queries: a structural validator + EXPLAIN pre-flight, and a cite-guard that traces drafted numbers back to returned rows. `validate_readonly_sql` rejects multi-statement/write/DDL, wraps in a guarded LIMIT, and runs `EXPLAIN` (not ANALYZE, so the query never executes); `relevant_traps` surfaces TRAP-marked schema comments before compose time; `quality_check` flags hazards (`join_without_key`, `comma_join`, `aggregate_over_nullable`, `no_profile_scope`, `empty_result`, …); `log_query` audits non-blocking; `run_adhoc_audited(conn, profile_id, sql, intent, limit)` is the standard entry point for any non-catalog query — validate, run, quality-check, and log in one call.

**safe_ops.py** — guards for manual corrections on already-logged data; prevents accidental bulk sweeps by date predicate. `today_utc()` is always UTC (matches the drain clock); `safe_bulk_update(conn, table, ids, update_data, dry_run=True)` and `reassign_day_by_ids(conn, table, ids, new_taken_at, dry_run=True)` both update by explicit id list only (raise on empty ids, default to dry-run), preserve the original timestamp in `notes`, and rely on Postgres to recompute the GENERATED `supplement_intake_logs.taken_on`. Reach for it when manually moving or fixing logged rows; never accepts a date predicate.

**models.py** — the single source of truth for Anthropic model ids (commit `bc672c9`): `SONNET = "claude-sonnet-4-6"` (the default everywhere per the one-model policy, commit `d4e2590`) and `HAIKU = "claude-haiku-4-5"` (kept available for per-path `system_config` overrides like `brief.model`, unused by default). `create_message(client, **kwargs)` wraps `client.messages.create` and turns a retired/invalid model id into a clear error instead of a silent 404. Every runtime Claude call routes through it; no other module may hardcode a `claude-*` id.

#### ingest/

**food.py** — interactive food logging with LLM macro estimation and Viome guidance cross-reference; implements the contract for `food_logs`. `classify_food(items, guidance_hits)` calls Claude (Sonnet via `models.SONNET`; `HS_FOOD_MODEL` env override) for a verdict (green/amber/red), confidence, and macros; `ingest_food_row(payload, conn, profile_id, method)` resolves per-item guidance hits → classifies → confidence-gates → validates → plain `INSERT` (manual entries have `source_log_path=NULL` and sit outside the partial unique index, so no upsert); `run_interactive` is the CLI loop. Food confidence is floored at 0.6 even with no guidance match (a plain meal is still valid).

**biomarker.py** — biomarker ingestion against the `metric_definitions` catalog; a thin wrapper over `contract.ingest_record` that adds a LOINC fallback. `ingest_biomarker_row(payload, conn, profile_id, method, attended)` resolves by metric name, falls back through `loinc_reference.loinc_code → metric_definitions.loinc_id` to recover the name, then delegates to the full contract pipeline; conflict key is `(profile_id, metric_definition_id, measured_at)`. Automated callers must pass `attended=False` to arm the unverified-value fail-safe (default is `True`).

**supplement.py** — supplement intake logging with alias resolution and regimen auto-attachment. `ingest_supplement_row` resolves via `supplement_aliases` (score 1.0) or `supplements.name` (0.9), auto-looks-up the active regimen, gates and validates, then upserts on `(profile_id, supplement_id, taken_on, source)`; helpers `find_active_regimen`, `list_active_regimens`, `close_regimen`, and `run_interactive` (CLI menu). Valid sources `{manual, journal, skill, csv, photo}` are checked early; `taken_on` is GENERATED — an ON CONFLICT target but excluded from INSERT.

**self_write.py** (v3.16.0) — direct self-write path for a NON-maintainer (whose `maintainer_ingest_*` call would 401) and the ONLY write path for `strength_logs`. `log_food`/`log_supplement`/`log_biomarker`/`log_strength(handle, profile_id, …)` insert the caller's OWN rows; RLS `has_profile_access` scopes the insert to self (a maintainer may also target a family profile). Driver-agnostic — duck-types `lib.db_rest.DbRest` (`.insert`, the App path), a psycopg2 connection (parameterised INSERT … RETURNING + commit), or a supabase Client. Truly GENERATED columns (`supplement_intake_logs.taken_on`, `strength_logs.performed_on`) are never sent. **`food_logs.log_date` is NOT generated** (no default, `is_generated=NEVER`) — `log_food()` sets it to the UTC date of `logged_at` (mirrors `maintainer_ingest_food`); omitting it inserts NULL and the row drops out of every `log_date = today` query (fixed 2026-06-14). The REST handle comes from `lib.db.get_app_db_rest(config)` (supabase_client mode). Maintainer food/supp/bio logging keeps the staging RPC path.

**whoop_sync.py** — pulls WHOOP API data (workouts, cycles/recovery, sleeps, body measurements) and upserts into `whoop_*` tables; uses stdlib `urllib` only (no httpx/requests). `sync_profile(conn, profile_id, since_iso, to_iso, env)` runs all four per-table syncs (each under a `wearable_sync_log` run with SAVEPOINT isolation); `refresh_recent(conn, hours, profiles, env)` is the mini-sync the morning brief calls (and exists specifically because WHOOP emits no cycle.updated webhook, so final `day_strain` must be re-pulled); `_map_cycle/_map_sleep/_map_workout` carry the migration-045 fields; `_resolve_access_token` prefers DB `whoop_tokens` then falls back to the `.env` refresh token. CLI `--backfill` pulls from 2020-01-01. All conflict keys use `(profile_id, whoop_id)`; zone fields are written only when `total>0` so screenshot-sourced zones aren't overwritten with zeroes.

**whoop_screenshot.py** — digitises WHOOP HR-curve screenshots via Claude vision; strictly additive, never overwrites API aggregates, captures the per-second curve and derived intervals the API can't provide. `process_image(conn, profile_id, png, ...)` runs vision extract → workout match → write samples/intervals → fill coach note/cardio split; `_match_workout` matches by local start time (±2 min) and duration and refuses to guess; `_derive_intervals(samples, z4_low)` detects work bouts and recovery valleys; `_already_done` is the idempotency check. z4_low comes from `hr_zone_config` (profile-specific, defaults 158); samples must be 30–230 bpm. Use it to enrich a matched workout, not to create one.

**whoop_oauth.py** — one-time WHOOP OAuth2 authorization_code consent flow (stdlib only); idempotent — skips the browser if the stored refresh token still works. `run_oauth()` completes the flow, returns an access token, and writes `WHOOP_REFRESH_TOKEN` to `.env`; `try_refresh(client_id, client_secret, refresh_token)` tests an existing token. Uses `authorization_code` only (WHOOP rejects `client_credentials`), `client_secret_post`, a CSRF `state`, and a localhost:8080 callback (120s timeout). Run once per new WHOOP-enabled user during onboarding.

#### monitor/

**inbox_drain.py** — the autonomous ingestion pipeline, triggered by GH Actions on `repository_dispatch inbox-drain`: reads `media_inbox`, atomically claims rows, downloads images from Supabase Storage, sends them to Claude Vision, writes via the `maintainer_ingest_*` SECURITY DEFINER RPCs, and sends Telegram confirmations. `run_once(db, cfg, api_key, token, settle_sec_override)` is the full loop returning a summary dict; `fetch_settled`, `build_clusters` (album grouping by `media_group_id`), `vision_extract`, `write_food/supplement/biomarker`, the `*_is_complete` predicates (auto-write vs force-stage), `lookup_food_reference/lookup_viome_verdicts`, `compute_meal_verdict` (worst-case: avoid>minimize>superfood>clean), and `compose_confirmation` (minor-safe). Note: it uses `HS_ANTHROPIC_API_KEY` (not `ANTHROPIC_API_KEY`); cluster claims are all-or-nothing; it late-imports `monitor.brief` and composes a brief for any profile that had a successful food write (post-log nudge) OR explicitly asked for one. **Minor consent gate:** a minor is briefed only if their profile_id is in `system_config` `brief.minor_optin_profile_ids` (mig 056; Dea opted in, Dev/future minors off by default) — applied to both the post-log and explicit-request triggers. Cross-run brief dedup (`brief.dedup_sec`, default 600s) collapses a burst of post-log briefs but is **bypassed for an explicit brief request** so "how am I doing?" always sends.

**brief.py** — composes and sends the structured daily Telegram health brief after food ingestion (macros vs targets, supplement adherence by slot, WHOOP recovery/HRV/sleep, Viome flags, 2–4 Claude-generated actions). `compose_brief(db, profile_id, cfg, api_key, token, today)` is the single entry point; private `_fetch_*` section fetchers all return empty on error (best-effort) and `_food/_supps/_whoop/_viome_section` render minor-safely. Default model `models.SONNET` (= `claude-sonnet-4-6`; per-path override via `system_config` `brief.model` — see §2.6); Viome is suppressed for minors; `PENDING_SCORE`/`UNSCORABLE` are shown with emoji rather than misleading zeros. CLI uses `ANTHROPIC_API_KEY` (inconsistent with inbox_drain — note when running locally). `--all` sends to active adults **plus any minor in `brief.minor_optin_profile_ids`**. Note: a CLI/event-driven send does not write the `push_log` brief ledger (only the drain's post-log path does), so it doesn't feed the cross-run dedup — bounded, since `send-brief.yml` has no cron.

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

Then grant the drain service account (`healthspan.drainer@chitalkar.com`) access to the new profile so the three `maintainer_ingest_*` RPCs pass the `has_profile_access()` guard when draining this user's photos. **Resolve the drainer's UID by email — never hardcode it** (the UUID previously printed here, `0b0e4093…`, was PC's auth identity, not the drainer's; following it would have granted the new profile to PC and the drainer would fail `has_profile_access()`, so the new user's photos would never drain):

```sql
INSERT INTO public.family_memberships (auth_user_id, profile_id, role)
SELECT u.id, '<new_profile_id>', 'owner'
FROM auth.users u
WHERE u.email = 'healthspan.drainer@chitalkar.com'   -- live UID: 6d69f36f-3ced-4164-a996-2110104e3594
ON CONFLICT DO NOTHING;
-- Grants the DRAIN account access to the NEW profile (auth_user_id = drain, profile_id = new user).
-- Same email-lookup pattern as migrations 035/053.
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

> ⚠️ **ALWAYS `deno check` before deploying.** `supabase functions deploy` bundles with esbuild,
> which strips types and will happily ship a file that **fails to boot at runtime** (HTTP 503
> `BOOT_ERROR` → every inbound update silently dropped). A duplicate `const` / redeclared variable
> is bundled cleanly but crashes the isolate. `deno check` catches it; the deploy CLI does not.
> ```bash
> deno check supabase/functions/<fn>/index.ts   # 0 errors (EdgeRuntime type-not-found is benign)
> ```
> After deploying, smoke-test it boots: `curl -s -o /dev/null -w "%{http_code}\n" -X POST
> <fn-url> -d '{}'` → expect **401** (alive, rejecting unauth), NOT **503** (BOOT_ERROR).
> (2026-06-12 incident: a duplicated `alertMaintainer` block 503'd telegram-webhook for ~17h.)

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
| `confidence_low` | LLM extraction confidence below `system_config.ingest.confidence_threshold` (0.3 in prod) | Re-send the photo at higher quality or with a caption |
| `missing_required` | A required field (e.g. `description`, `meal_type`) is NULL | Re-send with complete information |

To approve a staged item, write the corrected row directly to the production table using the appropriate `maintainer_ingest_*` RPC with `p_force_stage = false` and the corrected values. To dismiss, update the staging row to `status = 'dismissed'`.

### 7.8 Voiding a Mis-logged Row (soft-delete, mig 060)

A row that was logged in error (wrong day, hallucinated match, duplicate) is **voided, never
DELETEd** — health data is append-only and the audit trail must show the correction. As any
maintainer session (impersonate PC, or the skill's `supabase_client` connection):

```sql
SELECT maintainer_void_supplement('<row-uuid>', 'why it was wrong (≥5 chars)');
SELECT maintainer_void_food('<row-uuid>', '…');
SELECT maintainer_void_biomarker('<row-uuid>', '…');
```

Returns `{status: voided | already_voided | not_found}`. Every read path (brief, drain totals,
analysis, `lib/views.py`, `daily_health_summary`, ad-hoc SKILL queries) filters
`voided_at IS NULL`. **Un-void = re-log**: re-ingesting the same conflict key
(supplement: profile+supplement+day+source; biomarker: profile+metric+measured_at) clears the
void automatically. `healthspan_app` holds no DELETE grants (revoked in mig 060); there is
nothing to "hard-delete" outside the postgres role. `strength_logs` (mig 063) follows the same
pattern: `SELECT maintainer_void_strength('<row-uuid>', '…')` (no upsert key, so no un-void path).

### 7.9 Building a Per-Person Skill Bundle (v3.16.0)

Two bundle variants ship from one packager. The **leak guard always runs on the base bundle**
(repo hygiene); the per-person config is injected **post-guard** into the `dist/` zip only —
credentials come from gitignored `*.secret.txt` files resolved AT BUILD TIME (`@secret:FILE#KEY`
sentinels), so they never reach the guard scan or git.

**Restricted (Dea / App, supabase_client):**
```bash
# config/dea_app.secret.txt (gitignored), one per line:
#   auth_email=dea@chitalkar.com
#   auth_password=<Dea's Supabase auth password>
#   supabase_anon_key=<project publishable anon key>
python3 scripts/package_skill.py v3.16.0 \
  --person config/dea_app.config.example.json \
  --out healthspan-Dea-v3.16.0.skill
```

**Unrestricted (PC maintainer, privileged — bypasses RLS, DELETE/DDL on every profile):**
```bash
# config/pc_unrestricted.secret.txt (gitignored), ONE line — the privileged postgres URL:
#   postgresql://postgres:<pwd>@aws-1-<region>.pooler.supabase.com:5432/postgres
python3 scripts/package_skill.py v3.16.0 \
  --person config/pc_unrestricted.config.example.json --unrestricted \
  --out healthspan-PC-MAINTAINER-UNRESTRICTED-v3.16.0.skill
```
`--unrestricted` prepends the ⚠️ warning header to the bundle's SKILL.md. **Never share, sync, or
commit an unrestricted bundle or its secret** — the credential bypasses RLS for the whole DB.

**Verify each bundle** (after install, or locally against the assembled config):
`python3 scripts/self_test.py <config>` →
- Dea: `is_maintainer=False`, `distinct_profiles=1` (herself only), READY.
- PC unrestricted: `current_user=postgres`, `is_maintainer=True`, sees all profiles, READY (UNRESTRICTED).

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

Live verification — tick each as you confirm it in Telegram. Run as PC unless noted. **Groups A–E**
cover the 2026-06-08/09 logging & clarify changes; **Group F** covers the Telegram interactivity
shipped in v3.17–v3.19.1 (2026-06-11/12).

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
| ✅ **PASS** | **B3** | *(no message — DB check after B2)*: query `supplement_intake_logs` for Magnesium Bisglycinate | only ONE Magnesium Bisglycinate entry — *verified 2026-06-09 via DB: exactly 1 row (2026-06-08 22:43 ICT, 1 tab); the B2 clarify reply superseded the original staged row rather than logging both* | clarify supersedes, no dupe |

> **NOTE:** B2 MUST be an actual Telegram reply (long-press the bot's message → Reply), not a typed follow-up — that's the correlation mechanism.

### Group C — Photo path (consolidation didn't break it)

| ✓ | Test | Send | Expect | Proves |
|---|------|------|--------|--------|
| ✅ **PASS** | **C1** | a clear meal photo, caption "lunch" | ✅ logged with macros — *re-verified 2026-06-09 · drain@176c53b: image now GENUINELY READ (mig 053 storage RLS); French-press coffee photo logged 5 kcal. Earlier a2b2045 pass was caption-only — photos never reached the model until mig 053.* | photo path survives single extractor + image actually reaches vision |
| `[ ]` | **C2** | a nutrition-label photo ("this shake") | macros read from label (not "unidentified") — *image-read prerequisite now FIXED (mig 053); pending a label-photo test* | label reading + max_tokens fix |
| ✅ **PASS** | **C3** | a blurry/ambiguous food photo | 📋 asks what it is (clarify), not a wrong guess — *verified 2026-06-09 · drain@176c53b: ambiguous shake photo (confidence 0.18) STAGED + asked "reply here with what is in it"; reply re-extracted to ONE entry (180 kcal, 25g protein — honored user-stated macros, no double-count). Pre-mig-053 this silently auto-logged a 300-kcal guess.* | clarify-on-uncertainty on photos (stage hedged guesses) |
| `[ ]` | **C4** | reply to correct a confidently-**LOGGED** photo | replaces the entry, no double-count ("📥 Updating that — replacing…") — *LIVE — mig 054 + webhook v7 deployed 2026-06-09; ready for a live correct-a-logged-photo test* | supersede-on-reply (logged backstop) |

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

### Group F — Telegram interactivity (v3.17–v3.19.1, 2026-06-11/12)

| ✓ | Test | Tap / Send | Expect | Proves |
|---|------|-----------|--------|--------|
| `[ ]` | **F1** | Tap **📝 Update today** on a brief | expands to training toggles + supplement slot buttons (`💊 Morning 4/5`…) | two-level menu router (`menu:`) |
| `[ ]` | **F2** | Tap a supplement slot (e.g. `💊 Dinner`) | drills into that slot's individual pills (✅/⬜) + ⬅ back | slot drill-in (`slot:`) |
| `[ ]` | **F3** | Tap a pill (e.g. ⬜ NAC) | flips ✅ + count updates; tap again → ⬜ | supplement toggle (`supp:`, append-only void/un-void) |
| `[ ]` | **F4** | Tap a training activity (e.g. ⬜ hike) | flips ✅; persists across briefs | `goals.adherence_log` write (`tick:`) |
| `[ ]` | **F5** | Send `/whoop` | "🔗 Reconnect WHOOP" button → consent → "✅ WHOOP reconnected" | self-serve reconnect (one-time ticket) |
| `[ ]` | **F6** | Set `goals.daily_overrides[<date>]`, get that day's brief | training line reads "Today (…, override)" with the override sessions | daily_overrides supersede + marker |

### What pass means

Must-pass before relying = **A1–A4, B1–B2, C1, D1–D2**. **B2 is the keystone** (proves the clarify architecture). If any C test logs a wrong guess instead of asking, the confidence calibration needs a nudge. **Group F** is the current Telegram interactivity layer (menu + reconnect + overrides).

---

## 📜 Deploy log

### How versioning works

There is no separate release number — **the deployed git commit IS the version**. Every daily brief ends with a footer:

> `—v <date> · <commit7>` — e.g. `—v 2026-06-09 · 506a61c`

The `<commit7>` is the short SHA injected at build time from `GITHUB_SHA`, so any brief in a Telegram thread is traceable to the exact code that produced it. Two layers move independently:

- **Application code** — Python skill + Edge functions, tracked by the commits below. The brief-footer SHA reflects this layer.
- **Database schema** — forward-only numbered migrations (`migrations/NNN_*.sql`); the live DB state is the source of truth (there is no `schema_migrations` table). Latest applied: **mig 066** (2026-06-12, sprint goals atomic-writer RPCs — no new tables). Full DB-side history: §4 + `CLAUDE.md`.

### Recent deploys

Most-recent first. Curated to deploys that changed live behaviour — keep to the last ~15; full history is `git log`.

| Commit | Date | Type | Change |
|--------|------|------|--------|
| — | 2026-06-12 | fix | **Sprint goals atomic writers (v3.19.2, mig 066, telegram-webhook v10):** field-scoped `sprint_set_adherence`/`sprint_set_override` RPCs end the lost-update race where the PLANNING surface's full-object `daily_overrides` write reverted a TRACKING surface adherence tick (the 2026-06-12 'beach' revert). `lib/sprints.mark_done`/`set_override` + webhook `applyTick` all repointed. Ownership-gated; verified live (rolled back). |
| — | 2026-06-11 | feat | **Self-serve WHOOP reconnect from Telegram (v3.18.0, mig 065):** dead token → debounced "reconnect" button (whoop-webhook) or `/whoop` on demand (telegram-webhook) → `whoop-oauth?t=<ticket>` consent → token stored → "✅ reconnected" confirm. One-time `whoop_oauth_codes` tickets. Fixed the `offline`-scope bug in `ingest/whoop_oauth.py` (`e83de0b`). 3 functions deployed. Gating: register the edge redirect URI in the WHOOP dashboard. |
| — | 2026-06-11 | feat | **Daily-brief training plan + adherence (v3.17.0/.1):** `sprints.goals` object shape (`lib/sprints.py`); brief 🏋️ section with WHOOP-autoregulated directive (read from `goals.rules`); Telegram adherence ticks (`tick:` callback → `goals.adherence_log`, telegram-webhook v8). |
| — | 2026-06-11 | feat | **Bundle variants + write paths (v3.16.0):** privileged maintainer connection (`direct_role.privileged` → postgres role, RLS bypassed); non-maintainer self-write (`ingest/self_write.py`); strength write path; per-person bundle build (post-guard config injection). |
| — | 2026-06-11 | feat/schema/fix | **strength_logs (mig 063) + per-item Viome flags + supplement dedup (v3.15.0):** new `strength_logs` table (load/sets/reps/RIR, `performed_on` GENERATED UTC, soft-delete via `maintainer_void_strength`, RLS mirrored from `food_logs`); seed separate (`scripts/seed_strength_063.py`, `machine_chest_press` held). Drain now stamps Viome verdict/flags **per food item** not meal-wide (fixes "Superfoods: mushrooms ×5"); regression test added. Merged 4 duplicate learned supplements into canonical rows + confirmed 2 real ones. Suite 283/0/9. |
| — | 2026-06-10 | feat/schema | **Backlog batch (migs 060–062):** #18 void soft-delete (`maintainer_void_*` RPCs, read filters everywhere, healthspan_app DELETE revoked) · #20 WHOOP token-race recovery (Python + `_shared/whoop.ts`; whoop-webhook v17 + whoop-oauth v13 deployed) · #7 media retention prune (`media-retention.yml`, 45d) · #21 hygiene (INSERT policies scoped, legacy fns revoked, dead keys deleted, `claim_inbox_cluster` dropped, `hs_ops verify` rewritten runtime-derived). Suite 282/0/9. |
| — | 2026-06-10 | fix | **whoop-webhook v3 (BACKLOG #19):** `recovery.updated` id is the sleep UUID — resolve via `/v2/recovery` collection → integer `cycle_id`; fixed reversed `/v2/cycle/{id}/recovery` path. Verified live with a signed synthetic event; first-ever `recovery_landed` push sent. |
| — | 2026-06-10 | feat | **Clarify auto-match (BACKLOG #15, mig 059):** fresh-message answers to a pending clarify are LLM-matched and processed as the clarification; end-of-run orphan sweep retires staged items independently logged. 13 unit tests; live via CI on push. |
| — | 2026-06-10 | docs/schema | Deep-scan pass: mig 058 **applied** (stg_*_review RLS parity + `brief.dedup_sec` seed + version `rls_auto_enable`); SYSTEM.md §2.2/§2.3/§3.2 rewritten to LLM-routed text reality; §4.1 recount 45/16 of 61; whoop-webhook `recovery.updated` 404 bug documented (BACKLOG #19). Review queue cleared (8 orphans). |
| `e1af853` | 2026-06-09 | fix | Extraction prompt anchored to current date so partial dates keep the right year. |
| `930b734` | 2026-06-09 | test | 15 stale-contract drain/brief tests brought green (BACKLOG #16 closed) — test-files-only. |
| `05669dc` | 2026-06-09 | schema | `reticulocyte_count` metric_definition (mig 057) — last marker of the Jun 2026 Metropolis panel; reading 2.0% (normal). |
| `d2d5d3d` | 2026-06-09 | fix | Explicit brief request ("how am I doing?") bypasses the post-log dedup — only auto post-log briefs are deduped, so a request after a food-log isn't silently swallowed. |
| `13d51ef` | 2026-06-09 | feat | Minors get the daily brief via a per-profile consent allowlist (mig 056, `brief.minor_optin_profile_ids`; Dea opted in) — both post-log + explicit triggers; WHOOP now flows for her. Cleaned a phantom review-queue row. |
| `d4e2590` | 2026-06-09 | chore | One model everywhere — Sonnet 4.6 (dropped the Haiku split + `cluster_model` knob) |
| `bc672c9` | 2026-06-09 | fix | Central model registry (kill retired `claude-3-5-haiku` → 404; clear error on bad ids) + Haiku for clustering + drain concurrency guard + cross-run brief dedup |
| `ce91936` | 2026-06-09 | fix | Retire the `stg_food_log_review` row when a staged item is clarified (mig 055) — no more phantom review-queue entries. ✅ live (webhook v7 deployed 2026-06-09) |
| `7c635fe` | 2026-06-09 | feat | Reply-to-correct supersedes a logged food item (mig 054, no double-count) + stage ambiguous photos instead of auto-logging a guess. ✅ live (webhook v7 deployed 2026-06-09) |
| `390985c` | 2026-06-09 | fix | Storage RLS policy so the drain can read `health-media` images (mig 053) — the real reason photos never reached the vision model |
| `aad3f4d` | 2026-06-09 | fix | A photo is always a log, never a brief (C3) — enforce in drain + tests |
| `9964506` | 2026-06-09 | docs | Deploy-log sidebar block + Nanki (45F adult) in the multi-tenant model |
| `506a61c` | 2026-06-09 | feat | Learn-on-clarify for supplements + maintainer review + brief version footer |
| `ef43472` | 2026-06-09 | docs | Commands section + mark C1 / D1–D4 / B1 / B2 PASS; backlog #13 |
| `a2b2045` | 2026-06-09 | docs | Mark B1 / B2 PASS — clarify loop verified live |
| `ee434d8` | 2026-06-09 | fix | Food kcal plausibility floor 25→0 (mig 050) + backlog #12 (catalog gap) |
| `608467e` | 2026-06-09 | docs | Mark A1 / A2 PASS (webhook v6 / drain@main) |
| `ad87e24` | 2026-06-09 | fix | Energy line — WHOOP burn IS total (no BMR double-add) + brief context slug |
| `97d1f76` | 2026-06-09 | docs | Add Verification Tests checklist (§10) |
| `405f574` | 2026-06-09 | fix | `get_conn` uses `DATABASE_URL` env — refresh-on-interaction was silently failing in CI |
| `ac47c60` | 2026-06-08 | feat | Portion-scaled `food_reference` (#10) — `servings` multiplier scales the matched serving |
| `ccc2eec` | 2026-06-08 | refactor | Single extractor + enforced confidence gate (advisor clean pass, #11) |
| `2627d03` | 2026-06-08 | fix | Store `clarify_message_id` on the MAIN staged path (loop was broken) |
| `594aaff` | 2026-06-08 | fix | Vague supplement references clarify instead of fuzzy-matching a random row |
| `093de8a` | 2026-06-08 | docs | Sync migration 049 (clarify loop) into the manifest |
