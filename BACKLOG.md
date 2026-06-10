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

## #13 — `/learn` is advertised but not implemented (phantom command) — **OPEN, low**

**Severity:** LOW · **Owner:** CC · **Status:** OPEN (surfaced 2026-06-09).

After logging a repeat food the bot says *"Logged 'X' N× before — use /learn to add it to your
food library."* But there is **no `/learn` handler anywhere** — typing it does nothing (it just
routes as a normal message). The `promote_food_to_reference` RPC exists; wire `/learn` (in
telegram-webhook) to promote the most-recent food to `food_reference`, OR remove the offer text.
General rule: any command the bot suggests must be handled.

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

## #18 — No self-correct path: skill can log a bad row but can't undo it — **OPEN, MED**
*(renumbered 2026-06-10 — was a duplicate "#12")*

**Severity:** MED · **Owner:** CC · **Status:** OPEN. Surfaced 2026-06-08: a mis-dated NAC duplicate
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

## #15 — Ambiguous-photo clarify: non-reply correction orphans the staged item — **DESIGN Q, LOW**

**Severity:** LOW · **Where it bites:** PC's maintainer review queue (stale row after the item is already resolved).

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

## #19 — whoop-webhook: every `recovery.updated` event 404s (wrong id type) — **OPEN, MED-HIGH**

**Severity:** MED-HIGH · **Owner:** CC · **Status:** OPEN — found by the 2026-06-10 deep scan.

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

## #20 — WHOOP token refresh race: webhook vs CI `refresh_recent` (~1–2 token-400s/day) — **OPEN, LOW**

**Severity:** LOW (self-heals) · **Owner:** CC · **Status:** OPEN — found by the 2026-06-10 deep scan.

`wearable_sync_errors` shows `WHOOP token 400: invalid_request` ~1–2×/day across both PC
and Dea. WHOOP rotates the refresh token on every use; the webhook
(`_shared/whoop.ts getValidAccessToken`) and the Python `refresh_recent` (CI, every brief)
both refresh against the same `whoop_tokens` row, so a near-simultaneous refresh leaves
one side holding the stale rotated-out token → 400. Self-heals on the next event (tokens
verified fresh: both profiles refreshed 2026-06-10). Fix options: advisory lock /
compare-and-swap on `whoop_tokens.refresh_token`, or have the loser re-read the row and
retry once before erroring.

---

## #21 — Security/config hygiene batch (2026-06-10 scan, low severity) — **OPEN, LOW**

**Severity:** LOW · **Owner:** CC · **Status:** OPEN — consolidated from the scan's verified low-priority findings.

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
