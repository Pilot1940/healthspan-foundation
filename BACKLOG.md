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

## #3 — supplement_intake_logs.source CHECK constraint missing 'telegram' — **OPEN**

**Severity:** HIGH · **Owner:** CC · **Status:** OPEN — discovered during 039 live verification.

**Where it bites:** `supplement_intake_logs_source_check` allows only `['manual','journal','skill','csv','photo']`.
The drain's `_supplement_rpc_args()` sends `p_source='telegram'`, so any `force_stage=False`
supplement write from the Telegram drain will fail with a `CHECK constraint` violation.
In practice: every supplement that passes the completeness gate will fail silently at the DB level.

**Fix:** Migration 040 — `ALTER TABLE supplement_intake_logs DROP CONSTRAINT supplement_intake_logs_source_check; ALTER TABLE supplement_intake_logs ADD CONSTRAINT supplement_intake_logs_source_check CHECK (source = ANY (ARRAY['manual','journal','skill','csv','photo','telegram']));`
Also applies to `biomarkers.source` if it has a similar constraint (verify first).

---

## Done / shipped reference
- v3.2 (2026-06-05): supabase_client auth fix, lazy psycopg2, pinned cold-start, schema-map freshness, 924-char description. Webhook duration/zone-pct/sleep-fields fix deployed; prior-cycle refresh live.
- Pending elsewhere: migration 016c schema-comment completion (35 unmapped tables) — prompt already with CC.
