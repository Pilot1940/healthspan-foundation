# HealthSpan v3 — Packaging & Onboarding Runbook

The v3 build (V3-1 … V3-7 code) is complete, tested, and live on prod. This runbook covers
the three remaining V3-7 steps that need PC (real artefacts / a Supabase auth password):
**(A) skill-creator validation loop, (B) packaging, (C) onboarding Dea.**

---

## A. Skill-creator validation loop (on REAL artefacts)

Run each through the live skill, confirm the parsed rows, and check the outcome. All are
non-destructive to verify (use a throwaway/rollback or just review before confirming the write).

| Artefact | What to check |
|---|---|
| A real **DEXA** PDF/image | body-comp values land in `biomarkers`; android_gynoid 1.27-style values write + flag abnormal (not gated); any OCR/unit slip is staged `implausible`, not written. |
| A **supplement photo** | resolves against `supplements`/aliases; unknown item is FLAGGED for owner, never invents a catalog row; `analysis/supplement_summary` then lists it. |
| A few **catalog questions** ("recovery trend", "what's abnormal", "India vs travel") | answered from `lib/views`; every number cited to its source; NULL-recovery nights excluded. |
| **Create a goal** ("raise my VO2max to 55") | `plan/goals.create_goal`; target/unit from context or asked, never invented; `track_goals` shows direction-aware %. |
| **Build a plan** ("8-week base for a 10k") | `plan/training_plan.create_training_plan` with phases + weekly_template; `get_training_plan` reads it back. |
| A **comma-slip meal** (log "1,020 kcal" mis-OCR'd as 20) | `analysis/food_chat` stages it (implausible), surfaces "flagged for review", never writes 20. |

Iterate SKILL.md wording where the model misroutes; re-run. The unit suite (`pytest`, 107 green)
+ `python scripts/hs_ops.py verify` are the regression gates between iterations.

---

## B. Package the transportable `.skill`

```bash
python scripts/package_skill.py v3        # → dist/healthspan-v3.skill
```

- Bundles SKILL.md + CHANGELOG + runtime modules (lib/plan/monitor/analysis/ingest/export) +
  migrations + docs/SCHEMA-MAP + context templates + the **example** config.
- A **leak guard** aborts if any secret (filled config, `.env`, `*.secret*`, a real
  `hsapp_`/`sbp_`/JWT credential) reaches the staging tree. `dist/` is gitignored.
- Verify independently: `unzip -l dist/healthspan-v3.skill` — only `config/*.example.json`
  should appear under `config/`; no `.env`, no filled configs, no backups.

Each install still needs its own per-instance `config/<name>.config.json` (NOT packaged —
it carries the credential) + a `context/<name>.context.md`.

---

## C. Onboard Dea (needs a Supabase auth password — PC action)

Dea's profile, RLS, family membership, context MD (`context/dea.context.md`, `is_minor: true`),
and config (`config/dea.config.json`, `is_minor: true`) already exist and are verified. The one
thing that can't be minted autonomously is her **auth credential**:

1. In the Supabase dashboard (Auth → Users) set/reset a password for Dea's auth user
   (`47501376-6adb-458e-8679-57a4a4176692`, email `dea@chitalkar.com`), OR send a magic-link.
2. Dea's instance uses connection mode `supabase_client` (mobile/web — no Postgres credential on
   device): fill `supabase_url` + `supabase_anon_key` in her config and sign in with that
   password so the JWT carries `auth.uid()`; RLS scopes everything automatically.
   (Her current local config uses `direct_role` for testing — fine for PC's machine.)
3. Sanity after onboarding: under Dea's session, `monitor/ingest_health.check` → `maintainer:False`
   (no machinery), `trend_monitor` runs on her own data, and she sees **0** rows in
   `query_audit`/`wearable_sync_log`/`wearable_sync_errors`/`stg_*_review` (already proven in
   testing). Minor-safety framing comes from her context MD.

---

## Verified already (no action needed)
- Migrations 017–023 applied to prod; `hs_ops verify` green (no RLS leaks, 27/27 covered,
  5 maintainer-only tables, PC sees rows / a random user sees 0).
- Dea's full meal-log path proven under her RLS (writes food_logs + opens/closes a sync_log via
  the definer; comma-slip stages; she sees 0 maintenance rows).
- 107 unit tests green.
