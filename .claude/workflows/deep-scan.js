export const meta = {
  name: 'healthspan-deep-scan',
  description: 'Read-only deep audit of the HealthSpan production repo: 12 dimensions, each finding adversarially verified, then synthesized into a ranked report',
  phases: [
    { title: 'Context', detail: 'read docs/memory/backlog → known-issues + invariants digest' },
    { title: 'Scan', detail: '12 read-only dimension scanners (files + live DB)' },
    { title: 'Verify', detail: 'adversarially verify each finding (default: not real)' },
    { title: 'Synthesize', detail: 'dedup, rank by severity, produce report' },
  ],
}

// ── shared facts from the inline scout (so scanners don't re-derive them) ──────
const FACTS = `
LIVE DB SCOUT (2026-06-14, verified inline before this workflow):
- 63 base tables, 7 views. ALL tables have RLS ENABLED.
- All 7 profile_id-bearing views have security_invoker=true (daily_health_summary,
  daily_supplement_outcomes, effective_food_guidance, resolved_supplement_exposure,
  supplement_exposure_daily, v_india_vs_travel, whoop_journal) — mig 067 class is CLOSED.
- whoop_tokens: RLS ENABLED but ZERO policies (deny-all to authenticated; only service_role
  bypasses). Likely INTENTIONAL lockdown — VERIFY nothing using the 'authenticated' role needs it.
- healthspan_app role: ZERO table grants (DELETE revoked mig 060; appears fully inert now).
- 21 SECURITY DEFINER functions (incl. maintainer_ingest_*, maintainer_void_*, sprint_set_*,
  learn_supplement, lookup_*, fn_media_inbox_notify, has_profile_access, is_maintainer, rls_auto_enable,
  ingest_health_artifact + mint_ingestion_token (legacy, EXECUTE revoked from anon/authenticated mig 061)).
- 29 active system_config keys.
- No schema_migrations table: APPLIED DB STATE is source of truth. Migration files 001..069 (76 .sql files
  incl. helper/dryrun). hs_ops.py apply <file> is how migrations land.

NON-NEGOTIABLE INVARIANTS (CLAUDE.md):
1. Never hardcode thresholds — all from system_config.
2. AI extraction output → stg_* staging tables first, never straight to prod (the maintainer_ingest_*
   RPCs route to prod ONLY when complete + confident; the self_write.py path inserts a user's OWN rows directly).
3. Every new table needs an RLS policy in the same migration.
4. Test files mirror source (src/foo.py → tests/unit/test_foo.py).
6. Never modify auth logic without explicit instruction.

KNOWN / ACCEPTED (do NOT re-report as new bugs; you MAY report if you find them WORSE than documented):
- Write-side log_date/taken_on are UTC-derived; brief reads use a tz-local window (Asia/Bangkok). Known wart.
- 00:00–07:00 ICT edge: 📝-menu taken_on (UTC) vs brief local-day can differ for one row.
- Food→supplement bridge (mig 069, just shipped) is NOT wired to the reply-supersede loop (accepted).
- Supplements double-count on reply-correction (deferred; needs kind-agnostic link, not uuid[]).
- BACKLOG #13 (Telegram on-demand queries) OPEN by design (PC to scope). #12 catalog auto-add OPEN.
- #22 telegram-webhook credential-less test gap (OPEN).
- Drain identity satisfies is_maintainer() via family_membership to PC — load-bearing quirk (documented).
- Edge deploy gotcha: 'No change found' CLI skip + a duplicate-const BOOT_ERROR once dropped Telegram 17h;
  fix = deno check + curl 401-not-503 probe before trusting a deploy (SYSTEM.md §7.4).

RULES FOR EVERY AGENT IN THIS WORKFLOW:
- STRICTLY READ-ONLY. Never write/edit files, never apply migrations, never deploy, never run a DB write.
- DB access: cd into scripts/ and use  python3 -c "import hs_ops; hs_ops._load_env(); c=hs_ops.connect(); ..."
  (connect() defaults readonly=True — keep it readonly). Read .env via hs_ops only; never print secrets.
- For tests, run ONLY  python3 -m pytest tests/unit/ -q  (mocked, safe). Do NOT run write-capable scripts.
- Ground every finding in concrete evidence (file:line, a query result, a diff). No speculation without proof.
- Repo root: /Users/p.chitalkar/Library/CloudStorage/Dropbox/Development/HealthSpan/CODE/healthspan-foundation
`

const SCAN_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['dimension', 'summary', 'findings'],
  properties: {
    dimension: { type: 'string' },
    summary: { type: 'string', description: 'what was checked + overall health of this dimension' },
    findings: {
      type: 'array',
      items: {
        type: 'object',
        additionalProperties: false,
        required: ['title', 'severity', 'area', 'evidence', 'why_it_matters', 'suggested_fix', 'confidence'],
        properties: {
          title: { type: 'string' },
          severity: { type: 'string', enum: ['critical', 'high', 'medium', 'low', 'nit'] },
          area: { type: 'string', description: 'subsystem' },
          location: { type: 'string', description: 'file:line or table/function/migration name' },
          evidence: { type: 'string', description: 'concrete proof: code excerpt, query result, or diff' },
          why_it_matters: { type: 'string', description: 'production impact / blast radius' },
          suggested_fix: { type: 'string' },
          confidence: { type: 'string', enum: ['high', 'medium', 'low'] },
        },
      },
    },
  },
}

const VERDICT_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['is_real', 'is_already_known_or_accepted', 'adjusted_severity', 'reasoning', 'verification_method'],
  properties: {
    is_real: { type: 'boolean', description: 'true ONLY if independently confirmed a genuine issue' },
    is_already_known_or_accepted: { type: 'boolean', description: 'true if it matches a KNOWN/ACCEPTED item' },
    adjusted_severity: { type: 'string', enum: ['critical', 'high', 'medium', 'low', 'nit', 'not-an-issue'] },
    reasoning: { type: 'string' },
    verification_method: { type: 'string', description: 'exact file:line read or query run to confirm/refute' },
  },
}

// ── Phase 1: context digest ───────────────────────────────────────────────────
phase('Context')
const digest = await agent(
  `Build a tight CONTEXT DIGEST for a production deep-scan. ${FACTS}

READ these to ground it: CLAUDE.md (root), docs/SYSTEM.md (the full manifest — esp. §3 pipelines,
§4 schema/RLS, §7 runbook), BACKLOG.md (open vs shipped), and every file under
.claude/projects/*/memory/ (the auto-memory: MEMORY.md + the linked *.md). Also skim
docs/SCHEMA-MAP.md header for its generated_at stamp.

OUTPUT (concise, factual — this is fed verbatim to 12 scanners and their verifiers):
1. SYSTEM INVARIANTS the code must uphold (beyond the 6 listed) — anything load-bearing.
2. KNOWN/ACCEPTED ISSUES (canonical list, merged from BACKLOG + memory + CLAUDE.md) — so scanners
   don't re-flag them. Note which are OPEN-by-design vs latent-warts vs deferred.
3. RECENT CHANGES (last ~10 commits / current-state blocks) that a scanner should treat as "new and
   worth extra scrutiny" (e.g. mig 069 bridge, mig 066-068, v3.20.0 micronutrient).
4. ANY CONTRADICTIONS you notice between CLAUDE.md, SYSTEM.md, and the live-DB facts above.
Keep under ~700 words.`,
  { phase: 'Context' },
)

// ── Phase 2+3: scan each dimension, then verify each finding ───────────────────
const DIMENSIONS = [
  {
    key: 'schema-drift',
    prompt: `DIMENSION: Schema ↔ migrations ↔ docs drift.
Verify the live DB matches what the migration files and docs claim. Check: (a) every table/view/column
referenced by migrations 045–069 actually exists live (spot-check the recent ones thoroughly);
(b) no orphan tables (live tables not created by any migration) or phantom tables (migration-created but
missing); (c) docs/SCHEMA-MAP.md is not stale (generated_at >30d, or columns that no longer exist / new
columns missing) — note mig 069 added no columns so SCHEMA-MAP need not change for it, but 063/065 added
tables; (d) SYSTEM.md §4.1 table/view counts (63 base/7 views) and migration-history (001–069) match live;
(e) GENERATED columns (taken_on, performed_on, log_date?) match what code assumes.
Query live DB to confirm. Report drift only.`,
  },
  {
    key: 'rls-security',
    prompt: `DIMENSION: RLS & data-isolation security (HIGHEST PRIORITY — multi-tenant health data).
Check: (a) every profile-scoped table's RLS policies actually isolate by has_profile_access/profile_id —
look for any policy with USING(true) or missing WITH CHECK on INSERT/UPDATE; (b) whoop_tokens zero-policy
— confirm NO 'authenticated'-role code path reads/writes it (grep ingest/whoop_*.py, lib/db.py for the role
used; the drainer/CI use service_role?); (c) the 21 SECURITY DEFINER functions — each must gate on
is_maintainer()/has_profile_access() or be safe-by-construction; flag any DEFINER fn callable by
anon/authenticated that can act cross-profile; (d) storage RLS (health-media) — only the drainer can read,
end-users have none (mig 053); (e) maintainer-only tables (query_audit, stg_*_review, wearable_sync_*) deny
non-maintainers on SELECT *and* ALL. Use hs_ops to read pg_policy. This is where a leak hurts most.`,
  },
  {
    key: 'drain-pipeline',
    prompt: `DIMENSION: Drain ingestion pipeline correctness (monitor/inbox_drain.py — the core).
Read the whole file. Check: (a) per-item claim/release (claim_inbox_item CAS) — any path that claims a row
and then can return without marking it done/staged/failed (a stuck 'processing' leak)?; (b) the food branch
multi-item aggregate status logic; (c) the NEW food→supplement bridge (bridge_food_supplements,
match_regimen_supplements_in_text, _supp_trigger_phrases) — edge cases: empty regimen, dose=None,
discontinued regimen rows excluded, the noon-UTC pin vs the drain's UTC 'today', failure isolation; (d) the
clarify loop + orphan sweep + supersede (migs 049/054/055/059); (e) exception handling — any bare except
that could swallow a partial write; (f) the 'brief never from a photo' backstop. Flag real correctness/race
bugs, not style.`,
  },
  {
    key: 'brief-analysis',
    prompt: `DIMENSION: Brief + analysis + sprints correctness (monitor/brief.py, monitor/reconcile.py,
monitor/trend_monitor.py, analysis/*.py, lib/sprints.py, lib/views.py).
Check: (a) tz handling — local-day window over taken_at/logged_at consistent everywhere (any consumer still
using UTC log_date/taken_on where it should be local?); (b) voided_at IS NULL filter present on EVERY read
path (brief food/supps/Viome, totals, analysis, views, daily_health_summary) — a missing one shows voided
rows; (c) energy-balance (BMR + WHOOP activity, not just activity); (d) minor-safety framing (no
deficit/restriction language for is_minor) in brief + confirmations; (e) sprint goals normalize_goals
(object vs legacy flat array) + autoreg directive parse + adherence_log render incl. the new
iron/calcium/vitamin_d keys (mig 068). Flag real bugs.`,
  },
  {
    key: 'whoop',
    prompt: `DIMENSION: WHOOP pipeline (ingest/whoop_sync.py, whoop_oauth.py, whoop_screenshot.py,
supabase/functions/whoop-webhook, whoop-oauth, _shared/whoop.ts).
Check: (a) token refresh race handling (mig 060 fix present on BOTH Python _get_token_from_db and Deno
_shared/whoop.ts?); (b) the recovery.updated sleep_id→cycle_id resolution (whoop-webhook v3 fix) — no
remaining reversed /v2/cycle vs /v2/recovery call; (c) offline scope present in whoop_oauth (the refresh-
token bug e83de0b); (d) score_state handling (SCORED/PENDING/UNSCORABLE); (e) the oauth ticket flow
(whoop_oauth_codes single-use, TTL, RLS) — any way to attach a stranger's WHOOP to a profile?; (f) the
GATING manual step (redirect URI registration) — is it actually done now (memory says Dea reconnected
2026-06-12)? Flag real issues + the redirect-URI status.`,
  },
  {
    key: 'edge-functions',
    prompt: `DIMENSION: Edge functions Deno correctness (supabase/functions/telegram-webhook,
trigger-drain, whoop-webhook, whoop-oauth + _shared).
Run 'deno check' on each index.ts IF deno is available (which brew); else read carefully. Check: (a) any
duplicate const / shadowed binding (the BOOT_ERROR class that dropped Telegram 17h); (b) the callback_query
router (menu:/slot:/supp:/tick:/back:/close:) — every branch answers the callback + handles errors; profile
ownership verified before any write; (c) idempotency (telegram_processed_updates by update_id); (d) the
noon-UTC taken_at pin in toggleSupplement; (e) trigger-drain CAS dedup window; (f) any unhandled-promise /
missing-await that could 500. Flag runtime hazards.`,
  },
  {
    key: 'config-thresholds',
    prompt: `DIMENSION: Rule-#1 compliance (no hardcoded thresholds) + model ids.
Grep ALL of lib/ ingest/ monitor/ plan/ analysis/ export/ and the edge functions for numeric literals used
as thresholds/limits/windows that should come from system_config (compare against the 29 known keys).
Distinguish legit code-fallbacks (cfg.get(key, FALLBACK) — acceptable) from HARDCODED values with no
config key at all (a violation). Also: confirm model ids come from lib/models.py (HAIKU/SONNET) and nothing
hardcodes a retired model (e.g. claude-3-5-haiku-20241022 retired 2026-02-19). Check inbox_drain fallback
constants (_SETTLE_SEC_FALLBACK etc.) each have a real config key seeded. Flag true violations only.`,
  },
  {
    key: 'multitenant-minor',
    prompt: `DIMENSION: Multi-tenant correctness + minor safety + per-person bundles.
Check: (a) ingest/self_write.py (Dea's non-maintainer direct-insert path) — inserts only her OWN rows, RLS
scopes correctly, filters voided_at, no cross-profile leak; (b) lib/db.py privileged/restricted modes
(unrestricted maintainer connection bypasses RLS — gated only by the privileged credential); (c)
scripts/package_skill.py per-person bundle build — leak guard runs on BASE bundle, secret sentinels
(@secret:FILE#KEY) injected POST-guard, no secret committed; (d) minor framing end-to-end for Dea
(is_minor) — brief opt-in allowlist (brief.minor_optin_profile_ids), no deficit language, no Viome/learn for
minors; (e) Dea's micronutrient check-in (mig 068) path works over REST/supabase_client. Flag isolation or
secret-leak risks.`,
  },
  {
    key: 'test-health',
    prompt: `DIMENSION: Test health & coverage.
Run  python3 -m pytest tests/unit/ -q  and report pass/fail/skip. Check: (a) any FAILING test; (b) the 9
skips — are they legitimately env-gated (real-API/DB) or silently masking broken code?; (c) stale-contract
risk — do tests assert against current RPC signatures / function shapes (spot-check vs live DB)?; (d) the
test-files-mirror-source rule (Rule #4): list source modules in lib/ingest/monitor/plan/analysis/export
that have NO matching tests/unit/test_*.py; (e) the known #22 gap (telegram-webhook has no
credential-less tests) + webhook_test.ts coverage. Flag missing coverage on CRITICAL paths (drain, RLS,
ingest RPCs, the new bridge) most loudly.`,
  },
  {
    key: 'data-hygiene',
    prompt: `DIMENSION: Live data hygiene (READ-ONLY queries on production data).
Check via hs_ops read-only queries: (a) the review queue — count pending rows in each stg_*_review +
staged/pending media_inbox; are there orphans (the fresh-message-instead-of-reply pattern)?; (b) stuck
'processing' media_inbox rows (claimed but never finished); (c) duplicate supplement catalog rows
(near-dup names — the learn_supplement dedup gap); (d) voided rows still surfacing anywhere; (e)
wearable_sync_errors recent rate (WHOOP token-400s should be ~0 since mig 060; recovery.updated 404s gone);
(f) any food/supplement rows with implausible values that bypassed gates. Report counts + specific row ids
for anything actionable. Do NOT modify anything.`,
  },
  {
    key: 'migrations-hygiene',
    prompt: `DIMENSION: Migration apply-state & idempotency.
Since there's no schema_migrations table, verify each migration's DDL is reflected live. Check: (a) any
migration FILE whose objects are NOT present live (unapplied / partially applied) — focus on 060–069; (b)
idempotency — do migrations use IF NOT EXISTS / CREATE OR REPLACE / ON CONFLICT so a re-apply is safe (mig
069 should be ON CONFLICT)?; (c) any migration that creates a table WITHOUT an RLS policy in the same file
(Rule #3 violation) — grep CREATE TABLE vs CREATE POLICY per file for 045+; (d) the security-sensitive ones
(053 storage RLS, 058 stg parity, 067 view security_invoker, 069 config) actually took. (e) Helper/dryrun
.sql files that shouldn't be applied — confirm they're clearly non-migrations. Report apply gaps + Rule-#3
misses.`,
  },
  {
    key: 'deps-ci-ops',
    prompt: `DIMENSION: Dependencies, CI, ops/runbook integrity.
Check: (a) requirements.txt — pinned? any known-broken/yanked or security-relevant pins (e.g. the PyJWT
2.7.0 RECORD-conflict noted in docs)?; (b) .github/workflows/* (ci, inbox-drain, send-brief,
media-retention) — secrets referenced all exist as repo secrets per SYSTEM.md §6.2; concurrency groups
right; the drain checks out main (no ref) so feature branches don't auto-deploy — is that the intended deploy
model and is it documented?; (c) scheduled jobs (crons) align with docs; (d) SYSTEM.md §7 runbook commands
still valid (hs_ops subcommands exist, deno check step, deploy-marker bump); (e) any TODO/FIXME/XXX/HACK in
source indicating known-incomplete work. Flag ops landmines that could break a production run.`,
  },
]

phase('Scan')
const perDimension = await pipeline(
  DIMENSIONS,
  (d) => agent(
    `${FACTS}\n\nCONTEXT DIGEST (from the context pass — trust this for known/accepted items):\n${digest}\n\n` +
    `Now perform DEEP, READ-ONLY scan of ONE dimension and return structured findings.\n\n${d.prompt}\n\n` +
    `Be thorough: actually read the files and run the queries. Report ONLY genuine issues with hard evidence; ` +
    `an empty findings list is a valid, good result. Do NOT re-report KNOWN/ACCEPTED items unless materially worse.`,
    { label: `scan:${d.key}`, phase: 'Scan', schema: SCAN_SCHEMA, agentType: 'Explore' },
  ),
  // Verify every finding from this dimension independently, in parallel (no barrier between dimensions).
  (scan, d) => {
    if (!scan || !scan.findings || scan.findings.length === 0) {
      return { dimension: d.key, summary: scan?.summary || '(no scan result)', findings: [] }
    }
    return parallel(scan.findings.map((f) => () =>
      agent(
        `${FACTS}\n\nCONTEXT DIGEST:\n${digest}\n\n` +
        `ADVERSARIALLY VERIFY this single finding from the '${d.key}' scan. Your DEFAULT is is_real=false — ` +
        `only confirm if you INDEPENDENTLY reproduce the evidence by reading the cited file:line or running the ` +
        `cited query yourself. Refute it if: the evidence doesn't hold, it's already KNOWN/ACCEPTED, it's a ` +
        `false positive (e.g. a legit cfg fallback, an intentional service-role lockdown, a mocked test), or the ` +
        `code actually handles the case elsewhere. Be skeptical and precise.\n\n` +
        `FINDING:\n${JSON.stringify(f, null, 2)}`,
        { label: `verify:${d.key}:${(f.title || '').slice(0, 40)}`, phase: 'Verify', schema: VERDICT_SCHEMA, agentType: 'Explore' },
      ).then((v) => ({ ...f, dimension: d.key, verdict: v }))
        .catch(() => ({ ...f, dimension: d.key, verdict: null }))
    )).then((verified) => ({ dimension: d.key, summary: scan.summary, findings: verified.filter(Boolean) }))
  },
)

// ── Phase 4: synthesize ───────────────────────────────────────────────────────
phase('Synthesize')
const allFindings = perDimension.flatMap((r) => r.findings || [])
const confirmed = allFindings.filter((f) => f.verdict && f.verdict.is_real && f.verdict.adjusted_severity !== 'not-an-issue')
const refuted = allFindings.filter((f) => !(f.verdict && f.verdict.is_real) || f.verdict.adjusted_severity === 'not-an-issue')

const report = await agent(
  `You are the lead auditor writing the final DEEP-SCAN REPORT for a PRODUCTION health system (nothing can break).\n\n` +
  `CONTEXT DIGEST:\n${digest}\n\n` +
  `Per-dimension summaries (what was checked):\n${JSON.stringify(perDimension.map((r) => ({ dimension: r.dimension, summary: r.summary, n_confirmed: (r.findings || []).filter((f) => f.verdict?.is_real).length })), null, 2)}\n\n` +
  `CONFIRMED findings (verified is_real=true):\n${JSON.stringify(confirmed, null, 2)}\n\n` +
  `REFUTED / not-an-issue / already-known (for the appendix only):\n${JSON.stringify(refuted.map((f) => ({ title: f.title, dimension: f.dimension, severity: f.severity, verdict: f.verdict })), null, 2)}\n\n` +
  `Write a markdown report:\n` +
  `1. EXECUTIVE SUMMARY — overall health verdict + the single most important thing (if any).\n` +
  `2. CONFIRMED ISSUES — dedup across dimensions, rank by adjusted_severity (critical→nit). For each: ` +
  `title, severity, location, evidence, production impact, suggested fix, rough effort (S/M/L), and risk-of-fix ` +
  `(could fixing it break prod?). Use a table for the headline list, then per-issue detail.\n` +
  `3. AREAS VERIFIED CLEAN — dimensions/subsystems checked with no real issues (give confidence).\n` +
  `4. KNOWN/ACCEPTED (no action) — restate the accepted limitations so nobody re-litigates them.\n` +
  `5. APPENDIX: notable refuted findings (why they're false positives) — keep brief.\n` +
  `Be precise and conservative: for a production system, a wrong 'fix this now' is worse than a flagged maybe. ` +
  `Mark anything you're <high confidence on as "needs human confirmation".`,
  { phase: 'Synthesize' },
)

return {
  report,
  stats: {
    dimensions: DIMENSIONS.length,
    total_findings: allFindings.length,
    confirmed: confirmed.length,
    refuted: refuted.length,
    by_severity: confirmed.reduce((acc, f) => { const s = f.verdict?.adjusted_severity || f.severity; acc[s] = (acc[s] || 0) + 1; return acc }, {}),
  },
}
