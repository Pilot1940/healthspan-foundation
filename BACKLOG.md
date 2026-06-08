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

## #5 — Telegram clarification loop before LLM processes — **NEW, design wanted**

**Severity:** MED · **Owner:** PC (design) → CC (build) · **Status:** OPEN.

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

## #6 — Text-only messages can't log data (always route to brief) — **PARTIALLY SHIPPED**

**Severity:** HIGH (usage-dropper) · **Owner:** PC (design) → CC (build) · **Status:** PARTIALLY
SHIPPED 2026-06-08 — explicit `log:`/`add:`/`/log`/`/add` trigger now logs via text (PC chose the
additive option). Bare-text-defaults-to-log (full inversion) intentionally **deferred** — that's a
production-routing change needing the clarification loop (#5) + confirmation UX designed first.

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

## #7 — health-media bucket retention / cleanup (30–60 days) — **OPEN**

**Severity:** LOW · **Owner:** CC · **Status:** OPEN.

**Idea:** Photos pile up in the private `health-media` Storage bucket forever. Once a `media_inbox`
row is `done` (or `staged` and resolved) and the data is written to `food_logs`/biomarkers/etc.,
the source image has served its purpose. Periodically prune objects older than 30–60 days.

**Design:** scheduled job (pg_cron or a GH Actions cron) that lists `health-media` objects whose
linked `media_inbox.created_at` is older than the retention window AND `status IN ('done','failed')`,
then deletes the Storage object (keep the DB row for audit, null out `storage_path`). Make the
window a `system_config` key (`media.retention_days`, default 45) per the no-hardcoded-thresholds
rule. Skip anything still `pending`/`staged`/`awaiting_reply`. Log a count of pruned objects.

---

## Done / shipped reference
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
