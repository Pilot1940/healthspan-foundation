# HealthSpan — Backlog

Same schema as the BaySys repo backlogs: severity, where-it-bites, scope, status, owner.
Add items via PR or Cowork session.

---

## #1 — Morning-brief runner (daily reconciliation + Google Chat push) — **HELD: PC to finalise contents**

**Severity:** MED · **Owner:** PC (decision) → CC (build) · **Status:** HELD — full CC prompt drafted (below), do not run until PC picks the brief sections.

**Where it bites:** webhook misses self-heal only manually today (`python -m ingest.whoop_sync --since 7d --all-profiles`); no proactive morning surface.

**Decided already:** Option 1 (Python module `monitor/morning_brief.py` + GitHub Actions cron 07:30 IST), Google Chat incoming-webhook push (one-way — a two-way Chat bot is a separate build, deferred), reconcile-first via `refresh_recent` (48h, all profiles), render-guard cite-only numbers, maintainer block gated to PC, failures exit non-zero.

**PC to decide — candidate brief sections (pick/cut/add):**
1. Last night: sleep h, performance %, debt
2. Recovery / HRV / RHR vs 30-day baseline (delta)
3. Yesterday: strain + workouts (name, duration, zones)
4. Goal progress: active `user_goals` current-vs-target
5. Trend alerts (`trend_monitor` findings)
6. Today's targets from context MD (calorie split by day type, protein)
7. Maintainer block (PC only): staging counts, sync failures, reconciliation counts
8. Family line: Dea data-freshness / strap-compliance note
9. Supplement reminder line (regimen for today)
10. Training-plan line: today's scheduled session (program_workouts)

**Full drafted CC prompt:** see `Sys-Gen-1940` note in the Cowork session of 2026-06-05, or re-request from Cowork — covers morning_brief.py, the GH Actions workflow + secret names, webhook arrival-ping, tests.

---

## #2 — Per-sync "day catch-up" post (webhook-led) — **NEW, needs design**

**Severity:** LOW · **Owner:** PC (design) → CC (build) · **Status:** OPEN — design questions below.

**Idea:** every webhook-led ingest pushes a Google Chat post that *catches up the day so far* — not just "data arrived". E.g. on a workout event: the workout + day strain so far + calories vs today's target; on a sleep event: last-night summary, then recovery once scored.

**Design questions for PC:**
1. Which events trigger a post — all (sleep / workout / recovery), or workouts + recovery only?
2. Content per event type: what does "caught up on the day" mean — strain so far, kcal in vs target (needs food logs to be current), zone minutes vs weekly target, goal deltas?
3. Rate-limiting: WHOOP can emit bursts (re-scores, backfills) — debounce window (e.g. max 1 post per profile per 30 min)? Quiet hours?
4. Spaces: one family space, or per-person webhooks (Dea's posts to her own space — minor framing rules from her context MD apply)?
5. Where it runs: inside the Edge webhook (simple, but Deno + best-effort only) vs a queued worker (heavier, reliable). Recommendation: start inside the Edge function, try/catch so it never fails the ingest.

---

## #3 — supplement_intake_logs.source CHECK constraint missing 'telegram' — **DONE (040)**

**Severity:** HIGH · **Owner:** CC · **Status:** SHIPPED — migration 040, 2026-06-07.
Full write-contract audit also fixed: supplement supplement_id NULL guard, biomarker
metric_definition_id/value NULL guards, food meal_type/description guards. biomarkers.source
has no CHECK (verified) — no change needed there.

---

## #4 — Telegram-linked dashboards — **NEW, design wanted**

**Severity:** MED · **Owner:** PC (design) → CC (build) · **Status:** OPEN.

**Idea:** Design visual health dashboards that can be linked & available directly via Telegram
chat. PC sends/taps a command in Telegram → bot returns a dashboard (food, WHOOP recovery/strain/
sleep, supplements adherence, biomarker + weight trends) as a rendered image or a linked HTML page.

**Open design questions:**
1. Delivery format — rendered PNG (matplotlib/Pillow, sends inline) vs hosted HTML page (S3 +
   signed/short-lived link, opens in browser) vs both per-dashboard?
2. Which dashboards for v1 — daily snapshot, 7/30-day WHOOP trends, food+deficit, biomarker timeline?
3. Trigger surface — slash-style text commands to `telegram-webhook`, an inline keyboard menu, or
   scheduled auto-push (ties into #1 morning brief)?
4. Auth/scoping — per-profile (PC vs Dea), maintainer-only panels gated like the brief.
5. Reuse — share the brief's render-guard cite-only numeric path; no LLM-invented figures on a chart.

---

## #5 — Telegram clarification loop before LLM processes — **SHIPPED 2026-06-08, LIVE-VERIFIED 2026-06-09**

**Severity:** MED · **Owner:** CC · **Status:** SHIPPED + live-verified 2026-06-09 (drain@176c53b:
ambiguous shake photo staged + clarify asked, C3 PASS; reply re-extracted to ONE entry honouring
user-stated macros). Reply-to-clarify built (migration 049 +
`telegram-webhook` v6 + `inbox_drain` describe_stage). When an item stages, the bot sends an
LLM-written "what's unclear — reply to fix it" message and stores its Telegram message id; a user
REPLY re-queues the item (fresh INSERT so the AFTER-INSERT trigger fires) with the clarification +
original image appended, and the LLM re-extracts. `clarify_count` caps at 2 rounds → hands to PC.
Minor-aware (works for Dea with warm wording). **Acceptance test = one live reply round-trip in
Telegram** — PASSED 2026-06-09 (see CLAUDE.md live-test results). Design questions
below are answered by the implementation; kept for reference.

**Idea:** When an inbound ingestion is ambiguous or incomplete, Telegram itself should **ask the
user back for the missing details** (in-chat reply), and only **after** the user answers does the
LLM finalise the record. Turns today's silent "stage to `stg_*_review` for the maintainer" path
into an interactive, conversational clarification round-trip before write.

**Where it bites:** the completeness gate (mig 036/038) currently *holds* incomplete items in
staging with a `stage_reason` and waits for PC to drain them manually. The user who sent the photo/
text gets no prompt and no chance to supply the one missing field (e.g. portion size, meal type,
biomarker units) in the moment — so good data sits blocked.

**Proposed flow:** ingest → completeness/plausibility gate fails → instead of (or alongside)
staging, bot replies in Telegram with a **specific** question ("How many eggs? What time did you
take the magnesium?") → user reply lands as a follow-up message → correlated back to the pending
`media_inbox` / staging row → LLM re-processes with the new detail → writes to production.

**Open design questions:**
1. Correlation — how to tie the user's reply to the right pending item (reply-to-message-id,
   a short pending-item token in the bot's question, or "most-recent-incomplete-for-this-chat")?
2. Scope of questions — only the gate's `stage_reason` fields, or let the LLM ask anything it
   needs? Cap rounds (e.g. max 2 clarifications) to avoid loops.
3. Timeout / fallback — if the user never answers, after N hours fall back to today's staging-for-
   maintainer behaviour so nothing is lost.
4. State — track clarification state on `media_inbox` (new `awaiting_reply` status + question text)
   vs a small `ingest_clarifications` table.
5. Multi-item albums — ask once per ambiguous item, or batch the questions into one message?
6. Reuse the existing per-kind completeness gates (`*_is_complete`) to decide *when* to ask.

---

## #6 — Text-only messages can't log data (always route to brief) — **SHIPPED (superseded by LLM-intent routing, 2026-06-08)**

**Severity:** HIGH (usage-dropper) · **Owner:** PC (design) → CC (build) · **Status:** SHIPPED —
the original gap (bare text couldn't log) is closed by the LLM-intent routing below; the interim
regex trigger was removed the same day.

**Superseded by LLM-intent routing (2026-06-08).** The regex approach (`parseLogCommand`/verb
detection) was REMOVED — it gated text (a wrong guess permanently diverted to the brief, never
reaching an extractor), while photos always got the LLM. That asymmetry made every keyword patch
fail on the next phrasing. Now: ALL text → `media_inbox` (kind=unknown) → the drain's LLM is the
router. The `unknown` prompt classifies each message as a LOG (food/supplement/biomarker, multi-item)
or `{kind:"brief"}`; brief requests are composed inline at end-of-run (`compose_brief`). Supplements
match on `display_name` and default their dose from the user's regimen, so "i took D3, K2, B12,
magnesium citrate, omega-3" auto-logs all five. `guessKind` survives only as a photo-caption hint.

**Watch / not-yet-fixed (log, don't patch reactively):**
- Two text messages inside one settle window can be merged by `content_cluster_ungrouped` (e.g. a
  brief-request + a log in one cluster → one classification). Low-probability; new interaction from
  "all text → drain". Revisit if it bites.
- Ambiguous supplement names ("took my magnesium" with two magnesiums in the catalog) resolve to the
  first match — the clarification loop (#5) is the real fix.

**Where it bites:** `telegram-webhook` routes **every** text-only message to the daily brief
(`if (!isMediaMessage) → send-brief`). So "Supplements vitamin D3, vit K, magnesium, fish oil,
electrolytes" returned a *summary*, logged nothing — and the brief still showed `0/16 taken`.
Data can only be logged via photo/document today; freeform text logging is unwired. Users type
the most natural thing ("ate 2 eggs", "took my magnesium") and get nothing recorded → they stop.

**Proposed:** lightweight intent split on inbound text — if it parses as a log (food/supplement/
biomarker/weight via the same liberal classifier + a verb/noun heuristic) → enqueue for ingestion
(text → `media_inbox`-style row or a direct text-ingest path → LLM extract → staging/write); only
fall back to the brief for genuine summary requests ("brief", "how am I doing", "summary", "?").

**Open design questions:**
1. Disambiguation — keyword router vs a cheap LLM intent call vs an explicit `/brief` command for
   summaries and default-everything-else-to-log?
2. Text-ingest path — reuse `media_inbox` with a text body + null storage_path, or a new lane?
3. Confirmation UX — echo back what was logged ("✅ Logged: 2 eggs ~140 kcal") so it's not silent.
4. Safety — never execute caption text; treat as data only (already the rule for media captions).

---

## #7 — health-media bucket retention / cleanup (30–60 days) — **SHIPPED 2026-06-10**

**Severity:** LOW · **Owner:** CC · **Status:** SHIPPED — exactly per the design below:
`monitor/media_retention.py` (stdlib-only, service_role; per-object best-effort, exits non-zero on
hard failure) + `.github/workflows/media-retention.yml` (22:00 UTC = 05:00 ICT daily +
`workflow_dispatch` with dry-run input). Window = `media.retention_days` in `system_config`
(mig 062, seeded 45). Only `status IN (done,failed)` rows older than the window are touched;
Storage object deleted, `media_inbox.storage_path` nulled, row kept for audit; already-gone
objects (Storage 400/not_found) are nulled too. 6 unit tests; live dry-run verified (0 prunable
today — oldest media is 2026-06-07; first real prunes ~2026-07-22). Original design below.

**Idea:** Photos pile up in the private `health-media` Storage bucket forever. Once a `media_inbox`
row is `done` (or `staged` and resolved) and the data is written to `food_logs`/biomarkers/etc.,
the source image has served its purpose. Periodically prune objects older than 30–60 days.

**Design:** scheduled job (pg_cron or a GH Actions cron) that lists `health-media` objects whose
linked `media_inbox.created_at` is older than the retention window AND `status IN ('done','failed')`,
then deletes the Storage object (keep the DB row for audit, null out `storage_path`). Make the
window a `system_config` key (`media.retention_days`, default 45) per the no-hardcoded-thresholds
rule. Skip anything still `pending`/`staged`/`awaiting_reply`. Log a count of pruned objects.

---

## #8 — No scheduled WHOOP pull (only webhook + on-interaction) — **OPEN, low**

**Severity:** LOW · **Owner:** CC · **Status:** OPEN (mitigated 2026-06-08).

There is no GitHub Actions cron that runs `ingest.whoop_sync` — WHOOP data moved only via
the `whoop-webhook` push events (redeployed 2026-06-08) and the nightly ETL. Now also via
**refresh-on-interaction** (every brief calls `refresh_recent` first), which covers daily users.
Gap: a profile that never interacts won't refresh. If wanted, add a small cron workflow
(`whoop-sync.yml`, e.g. every 4h) calling `python -m ingest.whoop_sync --since 6h --all-profiles`
with the WHOOP_CLIENT_ID/SECRET + DATABASE_URL secrets (now set). Not urgent given the webhook +
on-interaction refresh.

---

## #9 — Localise `log_date` / `taken_on` at WRITE time — **OPEN, low**

**Severity:** LOW · **Owner:** CC · **Status:** OPEN.

Migration 048 + `brief.py` make the **brief** use the local day (`app.timezone`) via a `logged_at`
window. But `maintainer_ingest_food` still writes `log_date = (logged_at AT TIME ZONE 'UTC')::date`,
and `supplement_intake_logs.taken_on` is `GENERATED ALWAYS AS ((taken_at AT TIME ZONE 'UTC')::date)`.
So any **non-brief** consumer that filters by `log_date`/`taken_on` (skill ad-hoc queries, analysis
modules) still sees a UTC day boundary. To fully localise: update the food RPC to compute `log_date`
from `app.timezone`, and change the `taken_on` generated expression (drop/recreate the column — do it
carefully; it's an `ON CONFLICT` target). Not urgent — the brief (main surface) is correct.

---

## #10 — food_reference lookup ignores portion — **SHIPPED 2026-06-08**

**Severity:** MED · **Owner:** CC · **Status:** SHIPPED — the extractor returns a `servings` multiplier (half a shake → 0.5) and the drain scales the matched reference's macros by it, so a partial-portion log is no longer overwritten with the full serving (half Thai Tea shake = 95 kcal, not 190). Pending live verify.

When `lookup_food_reference` hits, the drain **overrides** the item's macros with the stored serving
(`food_item["calories"] = ref["calories"]` …), discarding any portion the user stated. So once
"Protein Thai Tea Shake" is in the reference (190 kcal/35P per 350ml bottle), logging "**half** a thai
tea shake" would write the FULL bottle's macros. Full-serving logs are now perfect; partial logs
regress. Fix: have the extractor return a `portion`/`servings` multiplier (default 1.0) and scale the
reference macros by it, or only apply the reference when no explicit partial portion is stated.

---

## #13 — Telegram on-demand queries & commands (expanded from the `/learn` phantom) — **OPEN, design — PC to scope v1**

**Severity:** MED (was LOW) · **Owner:** PC (design) → CC (build) · **Status:** OPEN — expanded
2026-06-10 per PC: don't just patch `/learn`; design the whole on-demand surface.

**Idea:** the bot currently *pushes* (brief, confirmations) but can't *answer*. Add an on-demand
query surface in Telegram — ask in natural language, get a cited answer:
- **"show me today's meals and macros"** → per-meal breakdown (description, kcal/P/C/F each) +
  totals vs targets — the VERBOSE version of the brief's Food section, on demand.
- **"what supplements have I taken today / what's left"** → regimen checklist by timing slot.
- **"how was my sleep this week" / "recovery trend"** → 7/30-day WHOOP mini-report.
- **Verbose review on demand** — a "full review" command returning the deep version of the daily
  brief (per-meal + per-supplement + WHOOP detail + Viome flags), where the auto-brief stays terse.
- **"undo last" / "void that"** — correction surface on top of the mig-060
  `maintainer_void_*` RPCs (the RPC layer SHIPPED 2026-06-10; only the chat surface is missing).
- `/learn` — the original phantom: after a repeat food the bot offers */learn* but no handler
  exists. Fold it into this command surface (wire to `promote_food_to_reference`), and keep the
  general rule: **any command the bot advertises must be handled.**

**Design questions for PC:**
1. Grammar: slash commands (`/today`, `/review`, `/learn`, `/undo`) vs pure natural language? The
   drain's LLM router (`unknown` prompt) could simply gain a `{kind:"query", question:…}` branch —
   no new infra; slash commands would short-circuit in telegram-webhook (faster, no LLM cost).
2. v1 scope: which 2–3 queries first? (today's meals+macros and the verbose review look highest-value.)
3. Answer path: reuse the brief's fetchers (`_fetch_food_full` etc., already voided-aware) vs
   `lib/views.py` catalog — either way numbers must be query-cited (render-guard rule), never composed.
4. Minor framing: Dea gets growth-framing answers, no deficit language (same rules as the brief);
   "undo/void" stays maintainer-only (PC).
5. Latency/cost: drain round-trip is ~30–60s (GH Actions). Acceptable for queries, or do the
   cheap lookups belong in telegram-webhook itself (Edge, instant, but Deno + no psycopg2)?
6. Overlap with #4 (Telegram-linked dashboards): text answers here, rendered charts there — same
   trigger surface should serve both eventually.

---

## #12 — Logging a supplement/product NOT in the catalog — **OPEN, med**

**Severity:** MED · **Owner:** CC · **Status:** OPEN (surfaced 2026-06-09 testing).

`supplement_intake_logs.supplement_id` is NOT NULL, so you can only log a supplement that
exists in the `supplements` catalog. A new product the user names ("Metabolic Shift Ketone
Electrolytes") never matches → the clarify loop asks name, then dose, then caps at 2 rounds
and hands to PC — it can never resolve because the row doesn't exist. Fix: when the user
confirms a name (and ideally a dose) for an unmatched supplement, AUTO-ADD it to the catalog
(`is_custom=true, owner_profile_id`) like `promote_food_to_reference` does for foods, then log
the intake. Workaround today: maintainer adds the catalog row manually.

---

## #11 — Extraction-prompt architecture (advisor review 2026-06-08) — **SHIPPED, pending live verify**

**Severity:** MED · **Owner:** CC · **Status:** SHIPPED 2026-06-08 (clean pass) — collapsed to ONE
extractor prompt (deleted the dead `supplement` prompt + drifted `food`/`lab`/`dexa` copies;
`vision_extract` always uses it; re-dispatch always unwraps `{kind,data}`); added a first-class
"WHEN UNSURE OF IDENTITY, DO NOT GUESS — confidence <0.3" escape; **enforced** the confidence gate
(food/supplement explicit-low-confidence → stage→clarify; safe default = log; threshold 0.7→0.3).
**NEEDS LIVE VERIFICATION** — food photo, multi-supplement, vague→clarify, brief; and the loop's
reply round-trip (everything routes into the clarify loop). Original findings kept below.

1. **Prompts force an output; none can say "I can't identify this — ask."** Principle to encode:
   clarify on **identity/portion** ambiguity, **estimate** on quantity (grilled-beef kcal is the
   job), and only ask when the missing detail *materially changes the record* (avoid clarification
   fatigue). Partially done: generic-supplement guard + "don't guess vague supplements" prompt note.
2. **`ingest.confidence_threshold` is stored but NEVER enforced** — it's read into `conf_threshold`
   and passed around, but nothing gates on it. Wire low-confidence → stage→clarify; that generalizes
   the hand-written generic guard.
3. **Dead / drifted prompts** (the maintainability root cause): the standalone `"supplement"` vision
   prompt is dead (`guessKind` never emits `supplement` — they only arrive via `unknown`); the
   standalone `"food"` prompt and `unknown`.food have **drifted** (rich label/decomposition rules in
   one, a shorter copy in the other). Text always uses `unknown`; photos sometimes `food`/`lab`/`dexa`.
   So a prompt fix on one path silently doesn't apply on the other — exactly the "fixed it, happened
   again" pattern. Collapse to the `unknown` prompt as the single extractor (verify `guessKind`
   outputs first), or delete the dead ones. **Risky on prod — sequence it, prove each step live.**
4. Optional: let the extractor return its own `question` for the clarify message (the model that saw
   the input asks better; saves the `describe_stage` call).

**Hard gate:** prove the loop's `staged → reply → logs` round-trip live before relying on any
clarify-routing change (descriptive feedback, generic guard, this) — they all dead-end if it doesn't fire.

---

## #18 — No self-correct path: skill can log a bad row but can't undo it — **SHIPPED 2026-06-10 (mig 060)**
*(renumbered 2026-06-10 — was a duplicate "#12")*

**Severity:** MED · **Owner:** CC · **Status:** SHIPPED — mig 060 applied + live-verified 2026-06-10,
exactly per the plan below:
- `voided_at`/`void_reason` on **all three** log tables (supplements + food + biomarkers — symmetry done
  in one pass since the read filters had to be touched anyway).
- `maintainer_void_supplement/_food/_biomarker(p_id, p_reason)` SECURITY DEFINER RPCs (maintainer +
  profile-access gated, reason ≥5 chars, idempotent). Live-tested in a rolled-back txn: void →
  already_voided → unauthorized-for-random-user all behave.
- **Un-void on re-log**: the supplement/biomarker ingest RPCs' ON CONFLICT DO UPDATE now clears
  voided_at — without this, re-logging the same (profile,supplement,day,source) would land on the
  voided row and stay invisible. Verified live (same txn).
- **Read filters everywhere**: brief (food/supps/Viome), drain running totals + orphan-sweep
  candidates, `analysis/supplement_summary` adherence, `analysis/food_chat`, `lib/views.py`
  (biomarker timeline / abnormal_labs / vo2_status), `plan/goals`, `lib/contract` plausibility
  heuristic, `daily_health_summary` view. SKILL.md ad-hoc rule added; SCHEMA-MAP regenerated
  (column COMMENTs carry the filter instruction).
- **`REVOKE DELETE ON ALL TABLES FROM healthspan_app`** — the 2026-06-08 grant had landed on ALL
  60+ tables (incl. profiles, system_config, whoop_tokens), worse than recorded below. Revoked;
  verified zero DELETE grants remain.
Chat/skill **surface** ("undo last" in Telegram) deliberately not built here — folded into the
#13 on-demand command design. Original finding below for reference.

Surfaced 2026-06-08: a mis-dated NAC duplicate
(`6276da9f`, source `skill`, dated June 7, superseded by the correct June 8 `66decb05`) could not be
removed by the skill — its role is barred from DELETE (by design) and there is no soft-delete column.
The row was UPDATE-flagged in `notes` as `VOID — …` as an interim, but it **still counts as an
intake** in every aggregation until properly voided.

**Fix — add a soft-delete column, NOT grant DELETE.** Health data should be append-only + soft-delete
(the audit trail should show "a mistake was made and corrected", not silently vanish), and the actor
issuing writes is an unattended LLM drain — raw DELETE on a hallucinated match is unrecoverable, a
stray extra row is not.

1. **DDL:** `supplement_intake_logs.voided_at timestamptz NULL` + `void_reason text NULL`. Consider the
   same on `food_logs` / `biomarkers` for symmetry (do supplements first).
2. **The actual work (not the DDL):** every READ/aggregation path must filter voided rows —
   `WHERE voided_at IS NULL` in the brief's adherence count, `query_log`, any supplement rollup.
   Adding the column alone is **cosmetic**; a voided dose still counts as taken until the reads filter.
3. **Do it via a SECURITY DEFINER RPC, not a raw grant.** Verified 2026-06-08: writes for
   `healthspan_app` go through definer RPCs (the role has INSERT=False/UPDATE=False on the base
   table); and the interactive skill runs `supabase_client` → queries as `authenticated`
   (DELETE=False, no RLS DELETE policy). A raw `GRANT DELETE TO healthspan_app` therefore (a) does
   NOT reach the supabase_client path the maintainer skill actually uses, and (b) bypasses the
   audited write-boundary. A `maintainer_void_supplement(p_id, p_reason)` (and/or
   `maintainer_delete_*`) SECURITY DEFINER function runs as owner → works for BOTH connection modes,
   stays inside `is_maintainer()/has_profile_access()` checks, and is auditable. Soft-void preferred
   over hard delete; expose `void`/`undo last` (Telegram + skill) on top of it.
4. **REVOKE the raw grant.** `REVOKE DELETE ON ALL TABLES IN SCHEMA public FROM healthspan_app;` —
   PC granted it 2026-06-08 ("as I manage it"), but verification showed it landed on the shared role
   (so Dea's minor connection + the unattended drain also got DELETE) and missed the maintainer
   skill's actual path. The RPC in (3) is the correct, scoped replacement.
5. Once shipped: confirm `6276da9f` stays gone (already hard-deleted via SQL editor 2026-06-08).

**Verified privilege state (2026-06-08, `supplement_intake_logs`):** healthspan_app
DELETE=True/INSERT=False/UPDATE=False · authenticated DELETE=False/INSERT=True/UPDATE=True · RLS
single ALL policy `has_profile_access(profile_id)`, enabled.

---

## Done / shipped reference
- **2026-06-08 — vision truncation + multi-item confirmation + Protein Thai Tea reference.** Bottle
  photo stuck in review = `max_tokens=1024` truncated the JSON (not a label-read failure) → bumped to
  4096 + prose-tolerant parse + retry; validated by re-driving the stuck photo to `done`. Multi-item
  confirmation fixed (`extracted = food_items[0]` discarded all but the first → "Logged 1" for a
  2-item log); now names all items + counts items not clusters. Added "Protein Thai Tea Shake" (190/
  35/9/2 per 350ml) to `food_reference` (global, verified, aliased) so future logs resolve instantly.
- **2026-06-08 — Brief is the LOCAL day + food-log corrections.** Migration 048 seeds
  `app.timezone='Asia/Bangkok'`; `brief.py` computes "today" as a local-day UTC window over
  `logged_at`/`taken_at` (fixes 07:00-ICT rollover + UTC display). Was surfaced by a meal logged with
  a future timestamp (22:45 ICT) — corrected, and meal_type/portions fixed on request.
- **2026-06-08 — WHOOP refresh-on-interaction + staleness fix.** No scheduled pull existed; data
  aged through the day and the stale flag wrongly compared cycle_start's UTC *date* to today
  (00:06 IST = 18:36 UTC prior day → false ">24h old"). Now: every brief refreshes WHOOP first
  (`refresh_recent`, best-effort); staleness is elapsed-time (>30h), tz-safe. whoop-webhook
  redeployed with `*.deleted` handling. WHOOP_CLIENT_ID/SECRET added as GH secrets + to both
  workflow envs.
- **2026-06-08 — vision label-reading + liberal food classification (was backlog A).** Root cause
  of the alley shake parking in review with null macros: (1) vision prompts never told Claude to
  read packaged nutrition labels; (2) "Add this shake" classified `unknown` ('shake' wasn't in the
  food regex) → used the weaker prompt. Fixed both: label-reading instructions (incl. Thai term
  mapping) in food + unknown prompts; `guessKind` expanded to a very liberal food net (drinks,
  shakes, packaged items, dishes, staples) reordered so genuine lab/workout/dexa still win; 27
  classification cases pass. `monitor/inbox_drain.py` + `telegram-webhook` (redeployed). The
  completeness gate was working correctly — it was the extraction that was blind.
- v3.2 (2026-06-05): supabase_client auth fix, lazy psycopg2, pinned cold-start, schema-map freshness, 924-char description. Webhook duration/zone-pct/sleep-fields fix deployed; prior-cycle refresh live.
- Pending elsewhere: migration 016c schema-comment completion (35 unmapped tables) — prompt already with CC.

---

## #14 — Minor daily brief via per-profile consent — **SHIPPED 2026-06-09 (commit `13d51ef`, pushed — LIVE via CI)**

**Severity:** — · **Owner:** PC · **Status:** SHIPPED — commit `13d51ef` is on `origin/main` (verified 2026-06-10), so it is live via CI.

**What:** Minors were excluded from the daily brief at both triggers (post-log nudge in `run_once`,
scheduled `send-brief --all`), so Dea got only "Tracked: …" confirmations — no food totals / WHOOP /
plan, and (because `refresh_recent` lives in `compose_brief`) no WHOOP refresh either. PC consented as
father+maintainer. Added `system_config` key `brief.minor_optin_profile_ids` (JSON array; mig 056) —
an explicit per-profile allowlist; Dea opted in, Dev / future minors stay off by default. Both gates now
admit a minor iff listed. Brief content already minor-safe. WHOOP refresh verified live for Dea.

## #15 — Ambiguous-photo clarify: non-reply correction orphans the staged item — **SHIPPED 2026-06-10 (two-layer auto-match, mig 059)**

**Severity:** LOW · **Status:** SHIPPED — PC decided 2026-06-10 ("I can be disciplined but we have a
14-year-old as a user"): absorb the fresh-message habit instead of fighting it. Two drain-side layers
(`monitor/inbox_drain.py`):
- **Layer 1 — `absorb_pending_clarify`:** a fresh TEXT message arriving within
  `clarify.match_window_sec` (900s) of a pending clarify for the same chat is LLM-judged
  ("does this answer the question?"); at ≥ `clarify.match_min_conf` (0.7) it is processed AS the
  clarification (original caption + `[clarification: …]` + original image) and the stranded staged
  item retires — exactly what a Telegram reply would have done. On any doubt → independent
  processing (a stranded orphan is recoverable; a wrong merge corrupts data). Reply path, photo
  messages, capped rounds (2), and profile mismatches all fall through unchanged.
- **Layer 2 — `sweep_staged_orphans`:** end-of-run safety net; a staged item whose profile logged a
  matching food/supplement prod row within `clarify.orphan_supersede_window_sec` (1800s) is
  LLM-matched and retired as superseded (`result_ref` → the prod row; review rows → `merged`).
  Catches the photo-follow-up pattern Layer 1 doesn't (all 5 historical orphans were this shape).
**Mig 059** seeds the three `clarify.*` keys + adds maintainer UPDATE policies on the five
`stg_*_review` tables (the drain passes `is_maintainer()` via its membership to PC's profile — same
mechanism as its `maintainer_ingest_*` calls — so it can retire review rows; Dea still cannot).
13 unit tests. The two-paths-for-a-minor design question below is MOOT for queue hygiene (orphans
self-heal); kept for reference.

**Where it bites (historical):** PC's maintainer review queue (stale row after the item is already resolved).

The 2026-06-09 incident: Dea's photo couldn't be identified → staged (review row `906847f9` + clarify
prompt msg 324). Her first correction "No that's banana bread" arrived as a **fresh message, NOT a
Telegram reply** to msg 324 — so the webhook's clarify-match (keyed on `reply_to_message.message_id`)
never fired, the staged photo row stayed `staged`, its review row stayed `pending`, and the text was
*independently* logged as food. She then replied properly to the confirmation → that superseded
correctly ("Sugar free banana bread", `d6c9fb01`). **Data cleaned 2026-06-09** (906847f9 → `merged`,
3f831e27 → `done`); item is correctly logged.

**Not a carry-through bug** (earlier hypothesis disproven — the match simply never fired). **No safe
narrow code fix:** linking a free-text message to a pending staged item without a reply pointer means
*guessing* which staged item it corrects → would mis-attach unrelated logs to a review row (strictly
worse than a stale row PC can dismiss). The genuine open question is a **design** one for PC: a minor's
ambiguous photo currently does two things at once — asks the minor to reply-clarify AND queues for PC.
Should those be one path or two? PC's call; not a code task until decided.

---

## #16 — The "15 environmental" unit-test failures are actually STALE-CONTRACT — **SHIPPED 2026-06-09 (commit `930b734`)**

**Severity:** MED · **Owner:** CC · **Status:** SHIPPED — fixed in commit `930b734`
("test: bring 15 stale-contract drain/brief tests green"), test-files-only, exactly per the
taxonomy below (no xfail/skip, prod 0.3 threshold used). Verified 2026-06-10: full unit suite
259 passed / 0 failed / 9 env-gated skips.

`tests/unit/test_inbox_drain.py` + `test_brief.py` have ~15 failures long labeled "environmental
(real-API 401 + prompt drift)". The scan **disproved that**: none are API/network failures — all are
deterministic assertions against code that intentionally evolved past them. Taxonomy:
- **Removed surface** (delete/rewrite): `write_supplement` wrapper gone (`test_write_supplement_*` ×3);
  `_VISION_PROMPTS` collapsed to one `unknown` key (`test_food_vision_prompt_*` ×2).
- **Redefined contract**: `supplement_is_complete` now id-only/dose-optional (`test_supplement_is_complete_*`,
  `test_supplement_missing_dose_stages`, `test_supplement_no_match_stages`); `summary["written"]` counts
  per-item not per-cluster (`test_food_list_writes_each_item`); learn-on-clarify/regimen-default
  (`test_unknown_kind_reclassified_as_food_list`); brief gained an unconditional `profiles` select
  (`test_brief_composes_from_mocked_data`); `vision_extract` error shape (`test_vision_extract_*`).
- **Product-significant** (`test_completeness_gate_complete_shake_autowrites`): the enforced confidence
  gate (commit `ccc2eec`) force-stages any item below the confidence threshold regardless of completeness.
  In PROD the threshold is `ingest.confidence_threshold = 0.3` (system_config), so a complete shake at
  conf 0.5 AUTO-LOGS — the test's hardcoded 0.7 overstates blast radius. Fix: assert against the prod
  value (0.3) or assert force_stage=True for the enforced gate.

**Fix, don't xfail** — there is no pending product fix behind these, so xfail would mislabel them as known
defects. Bring current in one `test:` commit. ~5 delete/rewrite, ~8 fixture/string repoint, 2 prompt-key
repoint (the meal_type CHECK invariant at `inbox_drain.py` is still live — keep that assertion).

## #17 — `biomarkers.source` has no controlled vocabulary / CHECK — **OPEN, LOW**

**Severity:** LOW · **Owner:** PC/CC · **Status:** OPEN (data-hygiene). The Jun-6 reticulocyte backfill
landed `source='lab_report'` while its 32 panel-siblings use `blood_test`. `biomarkers.source` is
documented free-text (mig 016 comment: "manual_import | manual_skill | lab report etc.") with NO CHECK,
so off-label values are silently accepted. Nothing consumes `source` today (no view/skill branches on
it), so zero functional impact — but intra-panel inconsistency is avoidable. Options: normalize the one
row to `blood_test`; optionally add a `system_config`-driven allowlist (rule #1) if `source` ever drives
logic. Tied to the broader provenance gap: this row also bypassed `stg_biomarker_review` (rule #2) as a
direct maintainer write, leaving no `staging_id`/`document_id` audit trail.

---

## #19 — whoop-webhook: every `recovery.updated` event 404s (wrong id type) — **SHIPPED 2026-06-10 (webhook v3, verified live)**

**Severity:** MED-HIGH · **Owner:** CC · **Status:** SHIPPED — fixed + deployed 2026-06-10.
WHOOP docs confirmed the v2 `recovery.updated` payload `id` is the **sleep UUID** ("the UUID of the
sleep that the recovery is associated with"); the handler now resolves it via the `/v2/recovery`
collection (keyed `sleep_id`, `limit=10`; not-found → ack 200 + `records_skipped`, no retry storm)
and fetches the integer `cycle_id`. Also fixed the REVERSED recovery-for-cycle path in both call
sites: `/v2/recovery/cycle/{id}` (always 404, silently swallowed — webhook-sourced cycle rows never
carried recovery fields) → `/v2/cycle/{id}/recovery` (verified 200 live). **End-to-end proof:** a
signed synthetic `recovery.updated` (real sleep UUID `991f1f65…` — the same UUID from the failure
log) produced the first-ever `webhook:recovery.updated` SUCCESS (1 upserted, recovery 51%/HRV 37.5)
and the **first `recovery_landed` push ever sent** (push_log 2026-06-10 07:04 UTC). Original
finding below for reference.

`supabase/functions/whoop-webhook/index.ts:407-410` routes `recovery.*` (and the
never-emitted `cycle.*`) events to `GET /v2/cycle/${id}` — but the WHOOP v2 recovery
webhook payload `id` is a **UUID**, while `/v2/cycle/` takes the **integer** cycle id, so
the fetch 404s and the handler 500s → WHOOP retries (each event appears 5–12× in
`wearable_sync_errors`). Live evidence: **110 `webhook:recovery.updated` failed sync-log
rows in the week to 2026-06-10, zero successes; `recovery_landed` pushes (incl. the
critical-recovery alert and HRV-crash check) have NEVER fired** — `push_log` holds only
`brief` and `workout_logged` rows. Data impact is contained: recovery still lands via
`refresh_recent` (brief pre-sync) and the sleep-event prior-cycle refresh.

**Fix direction:** mirror the working pull path (`ingest/whoop_sync.py` joins
`/v2/recovery` records on their integer `cycle_id`): on `recovery.updated`, fetch
`/v2/recovery?limit=…` (or the recovery-for-sleep lookup), take `cycle_id` → GET
`/v2/cycle/{cycle_id}` + map. Also verify the `/v2/recovery/cycle/${id}` path used at
lines 99/409 against the WHOOP docs (the documented v2 shape is
`/v2/cycle/{cycleId}/recovery`) — it is try/swallowed, so a wrong shape would silently
drop recovery scores on that path too. Then redeploy whoop-webhook (§7.4).

---

## #20 — WHOOP token refresh race: webhook vs CI `refresh_recent` (~1–2 token-400s/day) — **SHIPPED 2026-06-10**

**Severity:** LOW (self-heals) · **Owner:** CC · **Status:** SHIPPED — loser-recovers fix on BOTH
sides, 2026-06-10. On a refresh 400 the loser re-reads `whoop_tokens`: if the winner already stored
a fresh access token, use it; else if a rotated-in refresh token is present, retry ONCE with it;
anything else re-raises (no retry storm). Python `ingest/whoop_sync._get_token_from_db` (+ extracted
`_post_refresh` helper, 4 unit tests) and Deno `_shared/whoop.ts getValidAccessToken` —
whoop-webhook v17 + whoop-oauth v13 deployed 2026-06-10. Expect `wearable_sync_errors`
`WHOOP token 400` rows to stop. Original finding below.

`wearable_sync_errors` shows `WHOOP token 400: invalid_request` ~1–2×/day across both PC
and Dea. WHOOP rotates the refresh token on every use; the webhook
(`_shared/whoop.ts getValidAccessToken`) and the Python `refresh_recent` (CI, every brief)
both refresh against the same `whoop_tokens` row, so a near-simultaneous refresh leaves
one side holding the stale rotated-out token → 400. Self-heals on the next event (tokens
verified fresh: both profiles refreshed 2026-06-10). Fix options: advisory lock /
compare-and-swap on `whoop_tokens.refresh_token`, or have the loser re-read the row and
retry once before erroring.

---

## #21 — Security/config hygiene batch (2026-06-10 scan, low severity) — **SHIPPED 2026-06-10 (mig 061)**

**Severity:** LOW · **Owner:** CC · **Status:** SHIPPED — all 6 items, mig 061 + code, 2026-06-10:
1. **INSERT policies**: `media_inbox` + `push_log` scoped to `authenticated` +
   `has_profile_access OR is_maintainer` (the drain inserts `push_log` as the authenticated
   drainer → is_maintainer covers it; webhooks are service_role and bypass);
   `telegram_processed_updates` + `wearable_sync_errors` open policies DROPPED (writers are
   service_role / postgres-CI only). Note the scan's anon-key framing was wrong in one detail:
   anon holds NO table grants on these four — the live exposure was any *authenticated* session.
2. **Legacy mig-002 surface**: kept the functions, REVOKED EXECUTE from anon + authenticated on
   `ingest_health_artifact` + `mint_ingestion_token` (both had the Postgres default PUBLIC grant;
   `hs_ops mint-token` runs as postgres and still works).
3. **Dead config keys** deleted: `ingest.whoop_screenshot.direct_write`,
   `supplements.journal_intake_map`, `supplements.intake_source_priority`.
4. **`claim_inbox_cluster` dropped**; the misleading "Atomic claim — all rows or none" drain
   comment rewritten to describe the real per-item claim + compensation.
5. **`hs_ops.py verify` rewritten**: SUBJECT list now derived at runtime (every base table with a
   `profile_id` column — 42 of 61 vs the stale 32); leak check covers `with_check=true` writes
   anywhere + `qual=true` reads on profile-scoped tables (catalog read-alls counted, not failed);
   NEW maintainer-only assertion (SELECT **and ALL** policies on the 10 maintainer tables must
   gate on `is_maintainer` alone — would have caught the mig-022 miss). Live run: all NONE OK.
6. **`lib/models.py` docstring** fixed (`HS_FOOD_MODEL` env var, not `ingest.food_model`).
   The `test_telegram_webhook.py` live-DB self-skip gap is recorded below as #22.

---

## #22 — Deployed telegram-webhook has no credential-less test coverage — **OPEN, LOW**

**Severity:** LOW · **Owner:** CC · **Status:** OPEN (split out of #21.6, 2026-06-10).
`tests/unit/test_telegram_webhook.py`'s 9 tests need a live DB and self-skip in CI, so the deployed
webhook (v7+ supersede path) has no coverage in a plain `pytest` run. Options: a Deno test harness
for the Edge function, or mock the Supabase client in the existing tests.

1. **Open INSERT policies** (`with_check=true`, role public) on `media_inbox`, `push_log`,
   `telegram_processed_updates`, `wearable_sync_errors`. The intended writers use
   service_role (bypass RLS), so the policies serve no current writer; a leaked anon key
   could insert spam rows (a `media_inbox` insert triggers a paid drain run). Verify anon
   table grants, then tighten to `authenticated` + `has_profile_access` or drop.
2. **Legacy mig-002 ingestion surface**: `ingest_health_artifact` (EXECUTE granted to
   `anon`, token-gated, staging-only) + `mint_ingestion_token` — superseded by the
   Telegram path. Decide keep vs revoke/drop.
3. **Dead/orphan `system_config` keys**: `ingest.whoop_screenshot.direct_write` (gate
   never read by code), `supplements.journal_intake_map`, `supplements.intake_source_priority`
   (fossils of the dropped source-priority design, mig 028). Drop or `is_active=false`.
4. **`claim_inbox_cluster` RPC (mig 046) is never called** — the drain claims per-item via
   `claim_inbox_item` under a misleading "all rows or none" comment
   (`monitor/inbox_drain.py:~1485`). Either switch the drain to the RPC or drop it and fix
   the comment. (Mig 046's internal comments said "Migration 043" — fixed 2026-06-10.)
5. **`hs_ops.py verify` blind spots**: SUBJECT table list is stale (32 of 61 tables);
   its coverage heuristic can't distinguish maintainer-only SELECT from profile-wide ALL
   (it would NOT have caught the mig-022 miss fixed by 058). Derive the table list from
   information_schema at runtime + assert documented maintainer-only tables are gated
   solely by `is_maintainer()`.
6. **`lib/models.py` docstring** names a nonexistent `ingest.food_model` config key (the
   real override is the `HS_FOOD_MODEL` env var); `tests/unit/test_telegram_webhook.py`'s
   9 live-DB tests self-skip in CI, so the deployed webhook v7 supersede path has no
   credential-less coverage.

---

## #23 — Viome flags smeared across a photo cluster (meal-wide, not per-item) — **SHIPPED 2026-06-11 (v3.15.0)**

**Severity:** LOW (cosmetic) · **Owner:** CC · **Status:** SHIPPED.
A multi-dish photo stamped the meal-wide verdict + the FULL flagged-ingredient list onto EVERY
`food_logs` row, so one "mushrooms = superfood" ingredient rendered as "Superfoods: mushrooms ×5"
in the brief. `inbox_drain.py` now scopes the verdict/flags per inserted row (one viome lookup over
the candidate union, mapped back per item by its own `foods[]`/description terms); the Telegram
confirmation summary stays meal-level. Regression test
`test_food_list_viome_flag_is_per_item_not_meal_wide`. Today's 5 lunch rows corrected (only the
fettuccine keeps mushrooms/superfood). Live on push (CI runs `monitor/`).

---

## #24 — `learn_supplement` dedup is too literal (near-dup catalog rows) — **OPEN, LOW**

**Severity:** LOW · **Owner:** CC · **Status:** OPEN (data cleaned 2026-06-11; root-cause fix pending).
`learn_supplement` dedups by normalized name, so "B12 Methylcobalamin", "Vitamin B12 Methylcobalamin"
and the catalog "Methylcobalamin (B12)" all became separate rows; same for "Magnesium Biglycinate"
(typo) vs "Magnesium Bisglycinate" and "Fish Oil" vs "Omega-3 (EPA+DHA)". Cleaned manually 2026-06-11
(4 dups merged into canonical, intake logs repointed; 2 real new items confirmed). Fix: fuzzier match
in `learn_supplement` (token-set / key_active / alias check against the active catalog before
inserting), or surface a "did you mean <existing>?" confirm. Until then the brief's "Recently learned
(review)" list is the safety net.

---

## #25 — Common repeat items should never need clarification — **PARTIALLY SHIPPED 2026-06-11 (mig 064); broader follow-up OPEN**

**Severity:** MED (friction on daily logs) · **Owner:** CC (build) · **Status:** the three named items
SHIPPED 2026-06-11; the general auto-promote mechanism remains OPEN.
PC's frequently-logged items — the **Proten** Thai Tea, the Hooray lactose-free **banana** shake, and
his daily morning **TWT (The Whole Truth)** whey + creatine + glutamine shake — kept triggering
clarify/guess cycles. Root cause: `food_reference` matches by EXACT `lower(name)`/alias (mig 041), and
(a) the Thai Tea brand is literally "Proten" but the aliases only had "protein", (b) the banana flavour
had no row (the generic Hooray alias pointed at Strawberry's different macros), (c) the TWT shake had no
row at all. **Fixed (mig 064 + `scripts/seed_food_ref_personal_064.py`):** `brand='Proten'` + `proten*`
aliases on the Thai Tea row; new global Hooray Banana row (220/31); PC-personal TWT row (195/30, with
`twt`/`the whole truth`/`morning protein shake` aliases). All verified via `lookup_food_reference`.
**Still OPEN (broader):** match is exact, not fuzzy — a novel phrasing still misses; and there's no
food auto-promote-to-reference after N confirmed logs (the food analogue of supplement
learn-on-clarify). That generalisation is the remaining work — see #13.

---

## #26 — "📝 Update today" menu: toggles don't update the brief text / no clear confirmation — **A+C SHIPPED 2026-06-12 (telegram-webhook v11); B deferred**

**Severity:** MED (daily-driver confusion) · **Owner:** PC (picked A+C) → CC (built) · **Status:** **A + C SHIPPED** (telegram-webhook v11, 2026-06-12); **B (live brief-text re-render) deferred** — only build if the text-vs-buttons split keeps confusing.

**Shipped (A+C, webhook v11):** (A) supplement toggle toast now names the item + the slot's running
count (`✅ Magnesium · Morning 5/5` / `↩ …`); the `slot:` drill-in answers with a hint
("Tap a pill to mark it taken") so it no longer feels like a no-op. (C) `toggleSupplement` pins the
inserted `taken_at` to **noon UTC of the menu's date** (`${dateIso}T12:00:00Z`) so the GENERATED
`taken_on` lands on the menu's day and the just-tapped pill flips even in the 00:00–07:00 ICT window
(noon-UTC also sits inside the brief's Asia/Bangkok local-day window, so brief + menu agree).
**Still deferred (B):** the brief MESSAGE TEXT still doesn't re-render on toggle (only the keyboard does).

**Where it bites:** PC taps the supplement menu and it feels like nothing happened — "doesn't instantly
update supplements OR give a confirmation."

**Diagnosis (telegram-webhook `callback_query` router):**
1. **The brief MESSAGE TEXT never changes on a toggle.** Every callback re-renders only the inline
   *keyboard* via `editMessageReplyMarkup` ("the DB write is the source of truth, not the button face").
   But the brief text above the buttons shows the supplement checklist + slot counts (`☐ NAC`,
   `Morning:` …) frozen at compose time. The user reads the *text* and sees stale checkboxes → "didn't
   update," even though the DB write + the pill button face did flip.
2. **Top-menu slot buttons (`💊 Morning 4/5`) drill in SILENTLY** — `slot:` answers the callback with
   empty text (no toast) and just swaps to the pill submenu. Tapping one expecting it to mark the slot
   done feels like a no-op.
3. **Toggle confirmation is a transient, generic toast** — `answerCallback(cb.id, "✅ logged")` (no
   supplement name, not a modal); easy to miss.
4. **Latent date-edge:** the menu's "taken today" reads `taken_on` (GENERATED **UTC** date) while the
   menu's `dateIso` is the brief's **local** (Asia/Bangkok) day. During 00:00–07:00 ICT a fresh
   `toggleSupplement` insert lands on UTC-yesterday's `taken_on`, so the just-toggled pill won't flip —
   same root as the documented brief/menu tz caveat. Not the midday case PC hit, but it WILL bite in
   that window.

**Fix options (design fork — PC to pick):**
- **(A) Light:** name + count in the toast (`"✅ Magnesium · Morning 5/5"`), `show_alert:false`; make
  `slot:` answer with a hint toast; re-render the **top** menu after a supp toggle so the slot count
  moves immediately. No brief-text change. ~1 webhook deploy.
- **(B) Full:** also `editMessageText` the brief body on every toggle so the text checklist + counts
  reflect reality — requires re-composing the brief in the webhook (Deno) or calling back into the
  Python brief; heavier, and the brief composer is Python-only today.
- **(C) Fix the tz-edge** regardless: `toggleSupplement` should compute `taken_on` against the menu's
  local day (pass the intended date / set `taken_at` to local-noon), not raw UTC `now`.

**Recommendation:** A + C now (safe, high-value); B only if the text-vs-buttons split keeps confusing.
