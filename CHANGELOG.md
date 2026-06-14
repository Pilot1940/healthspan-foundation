# HealthSpan Skill — Changelog

## v3.20.0 — food-based daily micronutrient check-in (iron/calcium/vitamin D) (2026-06-13)

- **What:** a FOOD-based daily micronutrient check-in (iron, calcium, vitamin D) that renders
  in the daily brief with ✅/⬜ and is tickable over REST (supabase_client) — for Dea (minor,
  App bundle) who tracks these via FOOD, no pills. No `supplement_regimens` (pill-semantic,
  maintainer-locked catalog, supplement analytics don't run in App/REST mode).
- **Mechanism:** reuses the sprint adherence store (already REST-read/written + brief-rendered).
  `lib/sprints.py` gains `NUTRITION = [iron, calcium, vitamin_d]` (kept separate from the workout
  `ACTIVITIES`), a `🥗 Food check-in` line in `render_training_section` (renders for any active
  sprint; minor-safe fuelling framing), `mark_done()` now accepts the nutrition keys (already
  REST-capable via the `sprint_set_adherence` RPC — no psycopg2), and `new_sprint_goals()` seeds
  `food_checkin` so it persists into the next block.
- **mig 068:** `sprint_set_adherence` allowlist widened to also accept `iron|calcium|vitamin_d`
  (mig 066 hard-rejected non-workout keys). Function-definition change only; ownership gate and
  atomic subtree `jsonb_set` unchanged. Nutrition keys share `goals.adherence_log[date]` with
  workout keys but render on their own line.
- **SKILL.md:** dispatcher routes "had my iron foods / had dairy / got my vitamin D / logged my
  food supplement" → `mark_done(<key>)`; brief spec documents the food check-in (all profiles,
  FOOD not pills). **context/dea.context.md:** new "Daily micronutrients (via food)" section +
  an iron-status UNKNOWN flag (low HRV/high RHR could be low ferritin — surface until labs exist).
- Verified live in App/supabase_client mode under Dea's JWT: brief renders the line, an "iron"
  tick lands over REST and flips ⬜→✅, workout adherence still works, zero AttributeError. Suite 331/0/9.

## v3.19.2 — sprint goals atomic writers: no more lost adherence ticks (2026-06-12)

- **Bug:** PC's 'beach' adherence tick on 2026-06-12 silently reverted. Root cause = a
  lost-update race on `sprints.goals`. TWO surfaces write that one jsonb on the SAME row —
  TRACKING (Telegram ticks → `goals.adherence_log`) and PLANNING (the claude.ai skill →
  `goals.daily_overrides`) — and BOTH did a full-object read-modify-write. A tick landing
  between the planner's read and its write was overwritten by the planner's stale snapshot.
- **Fix (mig 066):** two field-scoped, atomic, ownership-gated RPCs —
  `sprint_set_adherence(sprint, date, activity, value, profile)` and
  `sprint_set_override(sprint, date, override, profile)` — each a single `jsonb_set` that
  merges ONLY its own subtree server-side, so the two surfaces can no longer clobber each
  other and two writes to the same subtree serialize on the row lock. SECURITY DEFINER,
  gated: authenticated callers need `has_profile_access`, service_role is trusted but the
  sprint is still pinned to the passed `profile_id` (defense in depth).
- **All three writers repointed:** `lib/sprints.mark_done` → `sprint_set_adherence`,
  `lib/sprints.set_override` (new helper for the skill) → `sprint_set_override`, and the
  Telegram webhook's `applyTick` → `sprint_set_adherence` (**telegram-webhook v10**).
  Verified end-to-end on live data (rolled back): a tick survives a concurrent override write;
  ownership gate rejects a wrong profile (both authenticated + service_role paths). Suite 319/0/9.
- **Supplement-menu UX (BACKLOG #26, telegram-webhook v11):** the "📝 Update today" supplement
  toggles felt dead — only the keyboard re-rendered (never the brief text) and the toast was a
  generic "✅ logged". Fixed (A+C of the agreed options): (A) toggle toast now names the item +
  the slot's running count (`✅ Magnesium · Morning 5/5`), and the slot drill-in shows a "Tap a
  pill to mark it taken" hint; (C) `toggleSupplement` pins the inserted `taken_at` to noon-UTC of
  the menu's date so the GENERATED `taken_on` lands on the menu's local day — the just-tapped pill
  now flips even in the 00:00–07:00 ICT window. Brief-text live re-render (option B) deferred.

## v3.19.1 — sprint daily_overrides (date-specific plan) (2026-06-12)

- `goals.daily_overrides` (optional): `{<YYYY-MM-DD>: {sessions[],intensity,hard?,recovery?}}`. The
  brief's training plan now uses `daily_overrides[today] or weekly_plan[weekday]` — a date override
  supersedes the weekday template for that one date only (`lib/sprints.todays_plan(goals, weekday,
  today_iso)`; `normalize_goals` carries the new key; legacy rows unaffected). Python-only, live on push.
- When an override is active the brief labels the line **"Today (Friday, override): …"** so it's clear
  the day's plan is a one-off, not the weekly template.
- **Docs:** captured the dual-surface model in SYSTEM.md §1 — claude.ai skill = PLANNING surface,
  Telegram bot = TRACKING surface, both writing the same RLS-scoped DB.

## v3.19.0 — two-level "📝 Update today" menu: training + supplements as buttons (2026-06-11)

**The brief stays one tidy button; tap it to tick training AND supplements from Telegram.**

- **Brief** now carries a single **`📝 Update today`** button (`lib/sprints.update_button`) instead of
  the flat training keyboard — shown whenever there's an active sprint or active supplement regimens.
- **telegram-webhook v9** — the `callback_query` handler became a small router (all rendered
  server-side from fresh data each tap):
  - `menu:` → top menu = training toggles (gym/beach/pool/hike/massage) + one button per supplement
    **timing slot** (`💊 Morning 4/5`, `💊 Dinner 3/4`, …).
  - `slot:` → drill into that slot's **individual pills** (✅/⬜ per supplement) + ⬅ back.
  - `supp:` → **toggle** that supplement for today, re-render the slot. Append-only: untake voids
    today's non-voided intakes (any source); take un-voids a prior row or inserts a fresh `telegram`
    one. Matches the brief's "taken today" (any non-voided intake).
  - `back:`/`close:` → up a level / collapse to the single Update button. `tick:` (training) now
    re-renders the full top menu.
  - Every op is scoped to the tapping chat's own profile (service_role + explicit profile_id).
- Two-level keeps the brief uncluttered no matter how many actions we add. Slot grouping + toggle
  verified live against PC's 16-supplement regimen (Morning 5 / Lunch 1 / Dinner 4 / Bedtime 2 /
  Anytime 5). Deno keyboard builders exported + tested; Python suite 21 sprint tests green.
- ⚠️ Minor: the menu's "taken today" uses `taken_on` (UTC date); the brief uses the tz-local-day
  window — they can differ for intakes logged 00:00–07:00 ICT. Acceptable for a tick UI.

## v3.18.0 — self-serve WHOOP reconnect from Telegram (2026-06-11)

**Dead WHOOP token? Tap a button in Telegram, log into WHOOP, done — no CLI, works for Dea too.**

- **Root-cause fix:** `ingest/whoop_oauth.py` was missing the `offline` scope, so a re-auth returned
  only a 1h access token (no refresh token). Added `offline` (commit `e83de0b`). The Deno `SCOPES`
  already had it.
- **Telegram reconnect flow (mig 065 + 3 edge functions):**
  - `whoop_oauth_codes` — one-time, expiring tickets (the ticket, not the raw `profile_id`, travels
    in the URL, so a shared link can't attach a stranger's WHOOP to a profile). RLS maintainer-only;
    edge functions use service_role. Config `whoop.oauth_ticket_ttl_min` (30), `whoop.reauth_alert_debounce_hours` (24).
  - **whoop-webhook:** when the refresh chain is dead (`getValidAccessToken` throws "re-auth needed"),
    sends a **debounced** "⚠️ WHOOP disconnected — tap to reconnect" button to the user's chat — instead
    of failing silently all day behind a stale-but-fresh-looking recovery number (the 2026-06-11 incident).
  - **telegram-webhook:** `/whoop` (or "reconnect whoop") replies on-demand with a reconnect button.
  - **whoop-oauth:** `?t=<ticket>` entry → peek → WHOOP consent → callback consumes the ticket
    (single-use) → exchange → store tokens → "✅ WHOOP reconnected" Telegram confirm. Manual
    `?profile_id=` path kept.
  - Shared ticket/keyboard/Telegram helpers in `_shared/whoop.ts`. Ticket single-use + debounce
    verified live. All three functions deployed.
- ⚠️ One manual step gates the live flow: register `…/functions/v1/whoop-oauth` as a redirect URI in
  the WHOOP developer dashboard (PC). Then `/whoop` → tap → consent works end-to-end (and fixes Dea).

## v3.17.1 — training-plan follow-ups: rule-sourced directive + Telegram adherence ticks (2026-06-11)

**Two loose ends from v3.17.0 closed.**

### Autoregulation directive reads from `goals.rules` (one source of truth)
- `parse_autoreg_directives()` extracts the `Green=…; Yellow=…; Red=…` text from the sprint's own
  autoregulation rule, so editing the sprint changes the brief with no code change. Falls back to
  `DEFAULT_DIRECTIVES` for legacy / differently-worded rules. PC's brief now shows his rule's exact
  wording ("downgrade hard->moderate").

### Telegram adherence ticks (wired `mark_done` end-to-end)
- The daily brief now carries an inline keyboard (`lib/sprints.adherence_keyboard`, 3+2 layout,
  ✅/⬜ per activity); `monitor/inbox_drain.telegram_send` gained an optional `reply_markup`.
- **telegram-webhook v8 (deployed 2026-06-11):** an additive `callback_query` branch handles a tap —
  parses `tick:<sprintId>:<date>:<activity>`, verifies the tapping chat owns the sprint
  (service_role bypasses RLS, so ownership is checked explicitly), sets
  `goals.adherence_log[date][activity]=true`, answers the callback toast, and refreshes the keyboard
  face. The message-ingestion path is untouched. Legacy array-shaped goals are normalized on write.
- Python `adherence_keyboard` + Deno `buildAdherenceKeyboard` share the exact layout/callback_data
  (≤64-byte limit verified both sides). Live write-path proven idempotent against PC's sprint.
  Suite 308/0/9.
- ⚠️ Telegram only delivers callbacks if the bot's `allowed_updates` includes `callback_query`
  (re-set the webhook with it if ticks don't register). Final live confirmation = tap a button on a brief.

## v3.17.0 — daily brief shows the training plan + adherence (sprints.goals jsonb) (2026-06-11)

**The Telegram daily review now renders today's training plan, autoregulated by WHOOP recovery.**

### `sprints.goals` is now an OBJECT (was a flat `string[]`)
- New shape: `{block_goals:[str], weekly_plan:{<weekday>:{sessions:[str],intensity:str,hard?,recovery?}},
  rules:[str], adherence_log:{<YYYY-MM-DD>:{gym,beach,pool,hike,massage}}}`. Legacy rows (still arrays)
  read safely via `normalize_goals()`. The `sprints_status` view now returns `goals->'block_goals'`
  for object rows so every existing consumer (dashboard, markdown report) keeps the goal-strings contract.

### New `lib/sprints.py` + Training brief section (`monitor/brief.py`)
- `_fetch_active_sprint` selects the sprint whose `[start_date,end_date]` contains today (profile's
  local day, latest start wins). `render_training_section` shows today's `weekly_plan[weekday]`
  sessions + intensity (HARD / recovery flagged), the WHOOP-autoregulated directive, and today's
  adherence ticks. Recovery bands: green ≥67 proceed / yellow 34–66 downgrade hard→moderate /
  red <34 pool+beach+massage — defaults overridable via `system_config`
  (`sprint.recovery_green_min`/`_yellow_min`), so no threshold is hardcoded in logic. Minor-safe
  (sessions/intensity only). The rest-of-day Claude actions now see the plan too.
- `mark_done(db, sprint_id, date, activity)` persists a Telegram tick into `goals.adherence_log`
  (read-modify-write over REST, or `jsonb_set` over psycopg2) — no DDL; `sprints` already has
  UPDATE + `has_profile_access` RLS. Multi-tenant: works for any profile with a sprint row (Dea once
  hers exists). 15 unit tests; suite 303/0/9.

## v3.16.0 — unrestricted maintainer bundle + non-maintainer self-write + strength write path (2026-06-11)

**Two new bundle variants and the write paths that make them work.**

### A. Unrestricted (privileged) maintainer connection (`lib/db.py`)
- `connection.direct_role.privileged: true` opens a connection that keeps its underlying role
  (`postgres`): it does **NOT** `SET ROLE authenticated`, so RLS is **bypassed** and DELETE/DDL
  across every profile is possible — the admin path the owner needs for cross-profile cleanup.
  The `request.jwt.claims` is still set so `is_maintainer()`/`has_profile_access()` resolve.
  Restricted mode (default, and `privileged` absent) is unchanged: `SET ROLE authenticated`, RLS on.
- ⚠️ This is gated only by possession of a privileged credential, which lives in a gitignored
  secret file and is injected per-bundle (never committed). The unrestricted bundle ships a loud
  warning header (prepended to SKILL.md at build time) and SKILL.md §7 now states the restricted
  guarantees explicitly do **not** apply in this mode.

### B. Non-maintainer self-write path (`ingest/self_write.py`)
- A non-maintainer (e.g. Dea) calling `maintainer_ingest_*` gets `unauthorized`. The new module
  inserts the user's OWN rows directly (`log_food`/`log_supplement`/`log_biomarker`); RLS
  `has_profile_access` scopes the insert to self, so a non-maintainer can only write their own data.
  REST handle via `lib.db.get_app_db_rest(config)` (supabase_client mode). Maintainer keeps the RPC path.

### C. Strength write path for everyone (`ingest/self_write.log_strength`)
- `strength_logs` (mig 063) had no ingest path. `log_strength(...)` inserts a set (load/sets/reps/RIR);
  `performed_on` is GENERATED and never sent; `load_unit` is kg|lb|bodyweight (never 'plates').
  Maintainer may target any family profile; a non-maintainer only their own (RLS). Reads filter
  `voided_at IS NULL`. Driver-agnostic (DbRest / psycopg2 / supabase client). 5 unit tests; suite 288/0/9.

### D. Per-person bundle build (`scripts/package_skill.py`) + self-test
- `--person CONFIG [--unrestricted] [--out NAME]`: the leak guard runs on the BASE bundle (repo
  hygiene unchanged), then the person's config — with `@secret:FILE#KEY` sentinels resolved from
  gitignored secret files AT BUILD TIME — is injected POST-guard into the dist/ zip only. The
  credential never reaches the guard scan or git. `--unrestricted` prepends the warning header.
  Committed templates: `config/pc_unrestricted.config.example.json` (privileged) +
  `config/dea_app.config.example.json` (supabase_client). Build runbook in SYSTEM.md §7.9.
- `scripts/self_test.py` now verdicts correctly for a maintainer: family-wide scope is EXPECTED
  (was falsely BLOCKED on "scope != 1 profile"); privileged mode asserts `current_user='postgres'`
  + `is_maintainer()` and sees all profiles.

## v3.15.0 — strength_logs + per-item Viome flags + supplement dedup (2026-06-11)

**A new training-log table, a fix for the "mushrooms ×5" brief smear, and a catalog cleanup.**

### `strength_logs` table (migration 063)
- New `strength_logs` table for resistance training — load / sets / reps / RIR per exercise group, with a `performed_on` GENERATED date (UTC, the immutable form copied from `supplement_intake_logs.taken_on`).
- Soft-delete via `voided_at`/`void_reason` + `maintainer_void_strength(p_id, p_reason)` SECURITY DEFINER RPC — byte-for-byte mirror of mig 060's `maintainer_void_food` (maintainer-gated, reason ≥5 chars, idempotent `already_voided`). Rows append; never DELETE.
- RLS + grants copied from `food_logs`: one FOR ALL policy on `has_profile_access(profile_id)`; `authenticated` gets SELECT/INSERT/UPDATE only (no DELETE, no `healthspan_app`). Verified live in the migration's own DO-block.
- Seed is **separate** (`scripts/seed_strength_063.py`) — the schema is transportable/multi-tenant, so PC's personal session never lives in the migration. PC's 2026-06-11 session seeded (trap-bar deadlift 165 lb 5×5, two cable lat-pulldown sets). The `machine_chest_press` row was recorded `load_unit='plates'`; PC confirmed 140 lb (2026-06-11), now seeded — all 4 sets in.

### Viome flags are now PER ITEM, not meal-wide (`inbox_drain.py`)
- A multi-dish photo used to stamp the meal-wide verdict + the FULL flagged-ingredient list onto **every** row in the cluster — so one "mushrooms = superfood" ingredient showed as "Superfoods: mushrooms ×5" in the brief. Now each inserted row gets only the verdicts whose ingredient is its own (one viome lookup over the union, scoped back per item by the item's own `foods[]`/description terms). The Telegram confirmation summary stays meal-level. Regression test added.

### Supplement catalog dedup (data)
- `learn_supplement`'s normalized-name dedup let near-duplicates through ("B12 Methylcobalamin" vs "Methylcobalamin (B12)"). Merged 4 learned duplicates into their canonical rows (2× B12, "Magnesium Biglycinate" typo → Bisglycinate, "Fish Oil" → Omega-3): repointed the intake logs, deleted the dup catalog rows. Confirmed the 2 genuinely-new learned items (ashwagandha gummy, ketone electrolytes) so the brief's "Recently learned (review)" list is clean. (Deeper fix — fuzzier dedup in `learn_supplement` — is backlog #24.)

### Common repeat items resolve deterministically (migration 064, BACKLOG #25)
- `food_reference` matches a candidate by EXACT `lower(name)`/alias equality, so PC's frequent items kept clarifying/guessing. Mig 064 fixes the two global branded rows: the Thai Tea brand is **"Proten"** (not "protein") — set `brand='Proten'` + `proten*` aliases; and added the distinct **Hooray Banana** lactose-free shake (220 kcal/31P — different carbs from the seeded Strawberry). PC's personal morning **"TWT" (The Whole Truth)** whey + creatine + glutamine shake seeded separately (`scripts/seed_food_ref_personal_064.py`, profile-scoped — not in the migration). Verified: "proten", "hooray banana", "twt", "the whole truth protein shake", "morning protein shake" all resolve via `lookup_food_reference`. Broader follow-up (food auto-promote-to-reference after N logs) stays backlog #25/#13.

## v3.14.3 — Single extractor + confidence gate + portion scaling (2026-06-08, advisor clean pass)

**The structural fix for "I fixed it but it happened again" — one prompt, one source of truth — plus clarify-on-uncertainty and portion-aware references. NEEDS LIVE VERIFICATION.**

### One extractor (`inbox_drain.py`)
- Collapsed `_VISION_PROMPTS` to a single `unknown` prompt used for every path (text + photo). Deleted the dead standalone `supplement` prompt (`guessKind` never emitted it) and the drifted `food`/`lab`/`dexa` copies. A fix now applies everywhere because there's one prompt. `vision_extract` always uses it; the re-dispatch always unwraps `{kind,data}` (the model's kind wins over the `guessKind` hint).
- Enriched it with the full food decomposition + nutrition-label rules AND a first-class **"WHEN UNSURE OF IDENTITY, DO NOT GUESS — confidence <0.3"** escape into the clarify loop.

### Confidence gate ENFORCED
- `ingest.confidence_threshold` was stored but never used. Now food/supplement items with an **explicit** confidence below it stage → clarify. Safe default: missing confidence = 1.0 (estimates don't stage — only the model's own "I'm unsure" does). Threshold 0.7 → **0.3** (clarify on identity, estimate on quantity).

### Portion-scaled references (#10)
- The extractor returns a `servings` multiplier (half a shake → 0.5, two bottles → 2). When a `food_reference` matches, the drain scales its macros by `servings`, so "half a Thai Tea shake" logs 95 kcal / 17.5P instead of the full 190 / 35.

### Reply-to-clarify storage fix
- `clarify_message_id` is now stored on the **main** supplement/food staging path (it was only on the parse-fail path — the live test caught the gap, so replies had nothing to correlate to).

## v3.14.2 — Reply-to-clarify loop + BMR-correct energy balance (2026-06-08)

**You can now REPLY to a staged item to fix it — the LLM asks what's unclear and re-extracts. Plus a real energy-balance bug fixed.**

### Reply-to-clarify loop (`telegram-webhook`, `inbox_drain.py`, migration 049)
- When an item stages, the bot sends an **LLM-written** "here's what was unclear — reply to fix it" line (instead of "Queued for review") and stores that message's Telegram id on the staged rows.
- **Reply** to that message → `telegram-webhook` INSERTs a fresh `media_inbox` row (original caption + `[clarification: …]` + the original image) so the AFTER-INSERT drain trigger fires (an UPDATE wouldn't — the trigger is INSERT-only); the original staged row is retired. The LLM re-extracts with the new detail. `clarify_count` caps rounds at 2, then hands off to PC.
- `describe_stage` is **minor-aware** — warm, simple wording for Dea, so the loop works for minors too (not just adults).
- Migration 049: `media_inbox.clarify_message_id` + `clarify_count` (additive).

### Supplements log without a dose (`inbox_drain.py`)
- `supplement_is_complete` now requires only a resolved supplement id; dose is optional (defaults from the regimen when present). Fixes Nutritional Yeast / Oral Lozenges (no regimen dose) staging every time.

### Energy balance fixed (`brief.py`)
- The brief did `intake − WHOOP_burn`, treating activity energy as the whole-day total and ignoring BMR — a 755 kcal activity burn read as a +1448 "surplus". Now `expenditure = BMR (calorie_floor from context) + activity` → e.g. 2203 in − 2690 out = −487 **deficit**. Flows into the Claude actions text so recommendations stop citing a fake surplus. Adult-only line (minors keep growth framing).

## v3.14.1 — Vision robustness + honest multi-item confirmations (2026-06-08)

**Fixes that surfaced during onboarding: a labelled-bottle photo stuck in review, and a 2-item log reported as "1".**

### Vision extraction (`inbox_drain.py`)
- `max_tokens` 1024 → **4096**. A labelled multi-item food ("170ml of this shake + a poached egg" with the label's vitamins in `foods[]`) overflowed 1024 tokens, truncating the JSON mid-string → "no parseable extraction" → stuck in review. Not a label-reading failure — the label was legible.
- `_extract_json()` tolerates code fences AND surrounding prose (outermost `{}`/`[]` substring). Truncated JSON still raises (that's the `max_tokens` case).
- Vision call **retries once** on a parse miss; logs `stop_reason`. Only stages if both attempts fail.

### Confirmations (`inbox_drain.py`)
- Multi-item logs named every item, not just the first: `extracted = food_items[0]` discarded the rest, so "pineapple and dragon fruit" (both logged) reported as "Logged: Pineapple / 1 logged". Now keeps the full list → "✅ Logged 2 items: Pineapple, Dragon fruit".
- `n_written` counts **items, not clusters** — "Done — N logged" and the run summary are now accurate for multi-item food/supplement.

### Reference library
- Added "Protein Thai Tea Shake" (190 kcal / 35P / 9C / 2F per 350 ml bottle) to `food_reference` (global, verified, aliased) so repeat logs resolve instantly without re-reading the label. Caveat: the reference overrides with the full serving — partial-portion logs aren't yet scaled (backlog #10).

## v3.14.0 — LLM-routed text logging + WHOOP refresh-on-interaction (2026-06-08)

**Natural-language Telegram logging with no commands, multi-supplement support, and fresh WHOOP on every brief.**

### Text routing — the LLM is the router (`telegram-webhook`, `inbox_drain.py`)
- Removed the regex gate (`parseLogCommand`/`guessKind`-for-text). It permanently diverted a mis-guessed message to the brief so it never reached an extractor — while photos always got the LLM. That asymmetry made every keyword patch fail on the next phrasing.
- All text-only messages now enqueue to `media_inbox` (kind=unknown). The drain's `unknown` prompt classifies each as a LOG (food/supplement/biomarker, multi-item) or `{kind:"brief"}`. Brief requests compose inline at end-of-run (reuses `compose_brief`). `guessKind` survives only as a photo-caption hint.
- Prompt hardened: an image is ALWAYS a log, never a brief (protects uncaptioned meal photos).

### Multi-supplement + robust matching (`inbox_drain.py`)
- Supplement branch rewritten as a multi-item loop (mirrors food): "i took D3, K2, B12, magnesium citrate, omega-3" logs all five.
- `lookup_supplement_by_name` matches `display_name` first (LLM returns "Magnesium Citrate", not `magnesium_citrate`).
- `lookup_regimen_dose` — a dose-less log ("took my magnesium citrate") defaults to the user's active regimen dose, so it auto-writes instead of staging.
- `compose_confirmation` made list-safe + multi-supplement aware (also fixed staged supplements wrongly saying "logged").

### Vision (`inbox_drain.py`)
- Food + unknown prompts now READ packaged nutrition labels (incl. Thai term mapping) — fixes meals/shakes staging with null macros.
- `guessKind` food net widened (drinks, shakes, packaged items, dishes, staples); lab/workout/dexa still win.

### WHOOP (`brief.py`, workflows)
- Refresh-on-interaction: every brief calls `refresh_recent` first, so on-demand briefs reflect the latest cycle/recovery (no scheduled pull existed).
- Staleness flag fixed: elapsed-time (>30h since `cycle_start`), tz-safe — was a UTC-date compare that falsely flagged a just-after-local-midnight cycle as ">24h old".
- `whoop-webhook` redeployed (`*.deleted` handling); `WHOOP_CLIENT_ID/SECRET` added to CI.

### Timezone (migration 048, `brief.py`)
- The daily brief's "today" is now the LOCAL day in `app.timezone` (`system_config`, seeded `Asia/Bangkok`), computed as a UTC window over `logged_at`/`taken_at`. Fixes the old UTC-date boundary that rolled over at 07:00 ICT (dropping 00:00–07:00 meals) and displayed times in UTC.
- Write-side `log_date`/`taken_on` remain UTC-derived (backlog #9).

## v3.13.0 — Ingredient-level Viome analysis + daily brief composer (2026-06-07)

**Composite dishes now decomposed into ingredients for Viome cross-check; worst-case meal verdict stored on food_logs; new `monitor/brief.py` sends a structured daily brief after every food write.**

### Vision prompt (inbox_drain.py)
- `_VISION_PROMPTS["food"]` extended: Thai/Indian/composite dishes must be decomposed into constituent ingredients in `foods[]`. Explicit instruction to surface: coconut milk/oil/flesh, dairy (ghee/paneer/butter/cream/cheese), alliums (onion/garlic/leek), high-sugar fruits, fatty meat cuts.
- Example: "Thai green curry" → `foods=[{name:"coconut milk",...},{name:"green curry paste",...},...]`

### Meal verdict (inbox_drain.py)
- `compute_meal_verdict(verdicts)` — worst-case logic: avoid > minimize > superfood > clean. Returns `(verdict_str, flagged_items_list)`. Minimize items included in flags when verdict=avoid.
- After food insert, one `db.update("food_logs", ...)` stores `verdict` + `flags[]` on the inserted row(s). Best-effort (logs warning, never raises).
- `inserted_ids` list tracks UUIDs of all successfully inserted food_log rows in the cluster loop.
- `_viome_verdict_lines` reformatted: avoid → `"⚠️ {item} — AVOID{reason}"` (was `"⚠️ {item} is on your AVOID list"`).

### Daily brief (`monitor/brief.py` — new file)
- `compose_brief(db, profile_id, cfg, api_key, token, today) → str`
  - Looks up Telegram chat_id + is_minor from `telegram_identities`.
  - Loads profile targets from `lib.context.load_context`.
  - **Food section**: full macro totals (kcal, protein, carbs, fat, meals) vs targets with remaining; minor framing.
  - **Supplements section**: active regimen by timing slot (morning/lunch/dinner/bedtime/anytime); ✅ taken / ⬜ not taken; current slot marked with ←.
  - **WHOOP section**: recovery%, HRV ms, RHR bpm, sleep h:mm; stale flag if `cycle_start < today`.
  - **Viome section** (adult only): today's food_logs rows with verdict ≠ 'clean'; surfaces flags[] for avoid/minimize + superfoods.
  - **Actions section**: 2-4 concrete rest-of-day suggestions from Claude (model: `brief.model` system_config key, default claude-haiku-4-5-20251001). Minor-safe prompt.
  - Sends via `telegram_send` (lazy import from inbox_drain, no circular import at module level).
- `_fetch_food_full` — full macro query (kcal + protein + carbs + fat; replaces inbox_drain's protein-only helper).
- `_fetch_supplement_status` — regimen + supplement name + today's intakes in three queries.
- `_fetch_whoop` — most recent `whoop_cycles` row with staleness flag.
- `_fetch_today_viome_flags` — today's food_logs where verdict not null and ≠ 'clean'.

### Hook in run_once (inbox_drain.py)
- After end-of-run summary messages: for each adult profile that had ≥1 food write (`per_chat[chat_id]["written"] > 0`), calls `compose_brief()` best-effort (import error → silent skip, exception → log warning only).

### CLI
- `python -m monitor.brief --profile-id <uuid> [--today YYYY-MM-DD]`

### Tests (8 new — 97 total across 2 files)
**tests/unit/test_inbox_drain.py** (4 new — 93 total):
- `test_green_curry_decomposes_to_coconut_milk`: foods[] with coconut milk → lookup_viome_verdicts called with "coconut milk" as candidate.
- `test_meal_verdict_worst_case_avoid_wins`: avoid + minimize verdicts → verdict='avoid', flags include both items.
- `test_meal_verdict_minimize_when_no_avoid`: minimize-only verdicts → verdict='minimize'.
- `test_meal_verdict_clean_when_no_flags`: empty verdicts → verdict='clean', flags=[].

**tests/unit/test_brief.py** (4 new — 4 total):
- `test_brief_composes_from_mocked_data`: full compose_brief flow with mocked DB; verifies telegram_send called, message contains food totals + WHOOP + supplement data.
- `test_brief_no_data_paths`: no telegram identity → returns '' and does not call telegram_send.
- `test_supplement_remaining_regimen_minus_taken`: _supps_section shows correct ✅/⬜ markers and taken count.
- `test_minor_framing_in_brief`: is_minor=True → no Viome section in message; is_minor forwarded to _call_claude_actions.

---

## v3.12.0 — Smart food resolution: macro reference + per-user Viome cross-check (2026-06-07)

**Shared macro library + per-profile Viome guidance; drain now resolves known foods to trusted macros.**

### food_reference table (migration 041)
- New table: `food_reference` — shared macro library. `profile_id IS NULL` = global (visible to all); `profile_id = <uuid>` = user's personal library.
- Columns: name, aliases[], serving_desc, calories, protein_g, carbs_g, fat_g, fiber_g, brand, profile_id, source, verified.
- Two partial unique indexes (case-insensitive): `food_reference_global_name` + `food_reference_personal_name`.
- RLS: global rows readable by all authenticated; personal rows by the owning profile only; write restricted to personal rows via authenticated (global = service_role only).
- `verified=true` = curated/trusted entry; `verified=false` = user-promoted (learn-from-past).
- Seed: **Hooray Strawberry Shake** (global, verified) — 340ml, 250 kcal, 31g P, 22g C, 4g F.

### Per-user Viome model (migration 041)
- `food_guidance` now has `profile_id UUID NULL REFERENCES profiles(id)`. `NULL` = global (family-wide); a UUID = belongs to that profile only.
- **All 86 existing rows** migrated to the maintainer profile (PC) dynamically via `profiles.is_maintainer = true` — no hardcoded UUID.
- `effective_food_guidance` view updated: added `AND (fg.profile_id IS NULL OR fg.profile_id = p.id)` filter so Dea's guidance excludes PC's Viome rows and vice versa.
- Old RLS `food_guidance_read` (`USING (true)`) replaced: global rows visible to all; personal rows to owner only.
- Design decision: nullable `profile_id` FK over a `scope TEXT` column (consistent with `food_reference`; FK + CASCADE enforces referential integrity).

### SECURITY DEFINER lookup RPCs (migration 041)
- `lookup_food_reference(p_name text, p_profile_id uuid)` — case-insensitive name/alias match; personal before global.
- `lookup_viome_verdicts(p_items text[], p_profile_id uuid)` — per-profile avoid/minimize/superfood lookup. DEFINER required because `effective_food_guidance` is `security_invoker=true`; calling it as the drainer service account would ignore the profile's personal guidance.
- `promote_food_to_reference(p_name, p_profile_id, macros…)` — upsert user-scope entry; ready for learn-from-past webhook handler (deferred — see below).
- `system_config` key `food_reference.learn_min_logs = 2` (Rule #1: no hardcoded thresholds).

### Drain changes (`monitor/inbox_drain.py`)
- `_food_reference_candidates(caption, food_item)` — builds ordered candidate list: caption first (user-typed brand), then vision description, then ingredient names from `foods[]`.
- `lookup_food_reference(db, candidates, profile_id)` — tries each candidate via RPC, returns first valid hit or None.
- `lookup_viome_verdicts(db, candidates, profile_id)` — bulk check via RPC, returns list of {item, effective_classification, reason}.
- `_viome_verdict_lines(verdicts)` — composes ⚠️ (avoid/minimize) or ✅ (superfood) lines for Telegram.
- `lookup_past_food_macros(db, description, profile_id, min_logs)` — checks `food_logs` for confirmed past entries; threshold from system_config.
- `_process_cluster` food branch:
  - For each food_item: try `lookup_food_reference` first; if hit, replace vision macros with reference macros.
  - After loop: collect all ingredient + description candidates → single `lookup_viome_verdicts` call.
  - If rpc_status=inserted, verdicts → append verdict lines to Telegram confirmation (adult only).
  - If no reference hit + inserted: check past logs → append learn offer if ≥ min_logs (adult only).
- `_process_cluster` signature: added `learn_min_logs: int = 2` keyword arg.
- `run_once`: reads `food_reference.learn_min_logs` from cfg; passes to `_process_cluster`.

### Tests (6 new — 89 total)
- `test_hooray_resolves_global_macros`: caption "Hooray" triggers lookup; reference macros (250/31/22/4) replace vision estimates; correct macros reach the RPC.
- `test_viome_avoid_food_flags_in_confirmation`: avoid verdict → ⚠️ + ingredient name + "AVOID" in Telegram message; correct profile_id forwarded.
- `test_viome_superfood_flags_positive_in_confirmation`: superfood → ✅ line; no ⚠️.
- `test_viome_verdict_suppressed_for_minor`: Viome guidance not surfaced to minor chats.
- `test_food_reference_candidates_caption_first`: caption is candidates[0]; no duplicates.
- `test_learn_from_past_offer_when_no_reference`: no ref hit + past macros → 💡 offer with count in message.
- **Note on scope isolation**: per-user isolation (PC's Viome not applied to Dea) is enforced by the `food_guidance.profile_id` FK + `effective_food_guidance` view filter + `lookup_viome_verdicts` SECURITY DEFINER SQL. The Python tests verify correct `profile_id` forwarding; actual RLS enforcement is a DB-level guarantee tested at the SQL layer.

### Learn-from-past (deferred)
- Drain detects and informs (💡 offer in Telegram message).
- `promote_food_to_reference` RPC is ready.
- The interactive `/learn` command (webhook handler for the actual promotion) is deferred to a follow-on PR once the Telegram webhook command parser is extended.

---

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
