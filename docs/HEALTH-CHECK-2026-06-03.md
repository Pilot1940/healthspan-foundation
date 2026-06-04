# HealthSpan — Health Check & Observability Pass (2026-06-03)

> Live inspection of the Supabase (PG 17.6) `public` schema + a cleanup/observability
> pass. Method: read-only inventory (`scripts/` one-offs) → backup → dry-run → apply →
> re-inventory verify. All DDL went through that discipline.
>
> **End state after this pass:** migrations **025** applied (024 drop + 025 query_audit
> reshape); **026 proposed but NOT applied** (dead-table drop — awaiting PC sign-off).

## 1. Headline

| Check | Result |
|---|---|
| Tables (public) | **66** (was 67 — dropped 1 stray backup) |
| Active (≥1 row) | 35 |
| Empty | 31 (classified §6) |
| Views | 7 |
| Foreign keys | 126 |
| **Orphan-FK rows** | **0** across all 92 in-schema FK columns (34 point at `auth.users`, skipped) |
| Invalid indexes | 0 |
| Un-validated (`NOT VALID`) constraints | 0 |
| RLS on profile_id tables | **100%** (36/36 base tables, see §3) |
| Maintainer flag | exactly 1 (PC), as designed |

No data integrity problems found. The schema is healthy; the work in this pass was
cleanup (one stray table) and wiring up observability (query logging).

## 2. Table inventory (active / dormant / dead)

**Active (35)** — carry live data:

`biomarkers` (379), `families` (1), `family_memberships` (5), `food_guidance` (22),
`food_logs` (98), `health_context_notes` (2), `healthspan_tests` (3), `hr_zone_config` (2),
`journal_behavior_aliases` (28), `journal_behaviors` (27), `loinc_reference` (55),
`metric_definitions` (171), `profiles` (3), `program_phases` (8), `sprints` (3),
`supplement_aliases` (25), `supplement_components` (7), `supplement_intake_logs` (17),
`supplement_regimens` (21), `supplements` (23), `system_config` (7), `test_definitions` (3),
`training_programs` (3), `travel_log` (15), `user_goals` (12), `users_extended` (1),
`wearable_sync_errors` (19), `wearable_sync_log` (66), `whoop_cycles` (870),
`whoop_journal_entries` (11070), `whoop_sleeps` (898), `whoop_tokens` (2),
`whoop_workouts` (653), `workout_hr_samples` (34), `workout_intervals` (13).

**Empty (31)** — classified KEEP / DEAD / UNCERTAIN in §6.

**Dropped this pass (1):** `food_logs_dedup_backup_20260602` (21 rows) — see §5.

## 3. RLS coverage

Every one of the 66 base tables has `ROW LEVEL SECURITY` **enabled**. Of the 36 base
tables carrying a `profile_id`, all 36 are scoped:

- **34** by a per-profile policy — `has_profile_access(profile_id)` (the standard
  household-access predicate), and for the maintenance tables also `is_maintainer()`
  (maintainer-only SELECT, owner-scoped INSERT — migrations 022/023): `query_audit`,
  `wearable_sync_log`, `stg_biomarker_review`, `stg_food_log_review`.
- `family_memberships` and `ingestion_tokens` scope by `auth.uid()` directly (they map
  auth identities, not health rows) — correct.
- **`whoop_tokens`** — RLS enabled with **NO policy** (default-deny) and all privileges
  REVOKED from `authenticated`/`anon`/`healthspan_app`. This is **intentional**
  (migration 015): it holds live WHOOP OAuth credentials, written/read **only** by the
  Edge functions via `service_role` (which bypasses RLS). The skill roles correctly see
  nothing. ✔ Not a gap.

**Intentional shared-reference reads (not a leak):** 14 reference/catalog tables expose a
`USING (true)` SELECT policy to `authenticated` — `canonical_aliases`, `food_guidance`,
`journal_behaviors`, `journal_behavior_aliases`, `loinc_reference`, `metric_definitions`,
`supplements`, `supplement_aliases`, `supplement_components`, `system_config`,
`test_definitions`, `test_targets`, `locations`, `log_type_config`. These carry no
`profile_id` (household-shared catalogs); world-readable-to-authenticated is by design.
`hs_ops verify` only scans the per-profile `SUBJECT` list, so it does not (and should
not) flag these.

## 4. Orphan-FK / constraint / index sanity

- **Orphan-FK scan** ran across **all 126** FK constraints. For the 92 whose parent is in
  `public`, every child value resolves to a parent row — **0 orphans**. The 34 FKs whose
  parent is `auth.users` were skipped (cross-schema, not inspectable from `public` alone).
- **Indexes:** 0 invalid/incomplete indexes.
- **Constraints:** 0 `NOT VALID` (un-validated) constraints. All CHECK/FK/UNIQUE are
  validated.

## 5. Stray backup table dropped (migration 024)

`food_logs_dedup_backup_20260602` — a one-off snapshot from the 2026-06-02 Phuket
`food_logs` de-dup. Pre-drop checks: dedup verified, full `pg_dump` backups present
(`backups/`), **no inbound FK** referenced it, and it had no RLS policy (not on any read
path). Dropped via `migrations/024_drop_food_logs_dedup_backup.sql` (backup →
dry-run-rollback → apply → re-inventory confirmed gone).

## 6. Dormant-table classification (the 31 empty tables)

### KEEP (17) — reserved for shipping/near-term v3 features or live infrastructure
| Table(s) | Why keep |
|---|---|
| `query_audit` | **Now live** — query observability (this pass, §7). |
| `stg_biomarker_review`, `stg_food_log_review`, `stg_food_rule_review`, `stg_supplement_intake_review`, `stg_test_result_review` | Staging tables. CLAUDE.md rule #2 mandates AI extraction lands in `stg_*` first; the ingestion contract writes here. |
| `ingestion_tokens` | Backs `hs_ops mint-token` / the `.skill` ingestion flow (live feature, just unused so far). |
| `trend_alerts` | Target for `monitor/trend_monitor.py` persisted findings. |
| `daily_logs`, `daily_log_metrics` | v3 daily-logging feature (in the RLS `SUBJECT` list). |
| `program_workouts` | Training-programs feature (`program_phases` is already active). |
| `biomarker_targets`, `test_targets` | Per-profile target bands (FK'd from the targets feature). |
| `weight_logs` | v3 weight tracking (in `SUBJECT`). |
| `food_rules` | Per-profile diet rules (`food_guidance` is the shared catalog; this is the personal layer). |
| `documents` | FK target for `biomarkers.document_id` and `wearable_sync_log.document_id` (source-PDF provenance). |
| `body_metrics_history` | Documented **dormant** in SCHEMA-MAP — `biomarkers` is the canonical home for DEXA/body-comp. Kept as the historical landing spot. |

### DEAD (8) — abandoned-SaaS fossils, proposed for drop in **026 (NOT applied)**
`user_invites`, `user_invite_tokens`, `user_tier_history`, `user_locations`,
`user_log_type_prefs`, `user_preference_history`, `brain_conversations`, `brain_messages`.

Fossils of the multi-tenant SaaS schema this project was forked from. Empty, referenced by
**no** live code path (`lib/`, `ingest/`, `monitor/`, `export/`, `scripts/`), and the v3
maintainer/family model has no use for them. Only inbound FKs are intra-set
(`brain_messages`→`brain_conversations`, `user_invite_tokens`→`user_invites`), so a single
`DROP TABLE` listing all eight is FK-safe with no `CASCADE` and no collateral.

> **Proposed drop:** `migrations/026_drop_dead_saas_tables.sql.PROPOSED`. Validated by
> dry-run (`BEGIN … ROLLBACK`): all 8 drop cleanly, `locations`/`log_type_config`/
> `user_telegram_links`/`audit_log` are untouched. **Left unapplied for PC sign-off.**

### UNCERTAIN (6) — need a PC ruling before classifying
| Table | Question |
|---|---|
| `user_telegram_links` | Telegram Bot is in the stated stack — is this the live link table or a fossil? Kept out of DEAD deliberately. |
| `audit_log` | Generic `user_id`-keyed audit (SaaS-era). Superseded by `query_audit` + `wearable_sync_log`? |
| `canonical_aliases` | Empty alias table — superseded by the populated `supplement_aliases` / `journal_behavior_aliases`? |
| `locations` | Shared-reference locations — is the travel model (`travel_log`) the replacement? |
| `log_type_config` | Config for a logging-types feature that may or may not ship. |
| `source_priority_config` | Source-priority/ranking config — planned or abandoned? |

## 7. Query logging (observability) — migration 025 + code

`query_audit` was ad-hoc-only and empty. It now logs **every** skill read:

- **Catalog runs** — `lib/views.run_view` logs `kind='catalog'`, `name_or_sql`=view name,
  `params`=view params, `row_count`. Pre-vetted, so no verdict and never flagged.
- **Ad-hoc runs** — `lib/sql_guard.run_adhoc_audited` logs `kind='adhoc'`, `name_or_sql`=
  the guard-validated SQL, `params={"intent": …}`, the quality `quality_verdict`, and
  `flagged` (any high-severity quality flag).

Schema (migration 025): `kind, name_or_sql, params, row_count, quality_verdict, flagged,
ts, profile_id` (+ `id`). The old `intent`/`sql`/`plan` columns were dropped — `intent`
folds into `params`, `plan` (EXPLAIN text) isn't needed for observability.

Both writes go through `lib.sql_guard.log_query`, which is **non-blocking**: the INSERT is
wrapped in a `SAVEPOINT`, so an audit failure (read-only connection, or a non-maintainer's
RLS) never breaks the read path. Verified live: a writable PC-scoped connection lands both
rows; a read-only connection still returns view rows and the audit cleanly no-ops.

RLS is unchanged — `query_audit` SELECT stays **maintainer-only** (PC), INSERT owner-scoped
(migration 022). Dea never sees the trail.

`monitor/query_log.py` gives the maintainer a session-start digest: recent query counts
(catalog vs ad-hoc), most-used views, and anything flagged. Same maintainer gate as
`monitor/ingest_health.py`.

## 8. Doc-vs-DB drift

- **Migrations:** files run sequentially via `hs_ops apply` (no `schema_migrations`
  tracking table — applied state is the source of truth). 021→022→023 verified live
  (`query_audit` exists, `profiles.is_maintainer` column present, `is_maintainer()`
  resolves via `family_memberships`). 024 + 025 applied this pass. 026 authored but not
  applied.
- **SCHEMA-MAP.md** documents the ~28 skill-relevant tables (the ones the skill composes
  SQL against), not all 66. The undocumented remainder are reference/config catalogs and
  the SaaS fossils — expected, not drift. SCHEMA-MAP regenerated this pass
  (`scripts/gen_schema_map.py`).
- **Pre-existing test failure (out of scope):**
  `test_context.py::test_pc_calorie_target_from_context_not_a_default` fails (expects
  2514, gets 2100) because `context/pc.context.md` has an **uncommitted** edit changing
  PC's calorie target. That's a data edit, not part of this pass — left untouched and
  **not** committed here. Flag to PC.

## 9. What changed in this pass
- `migrations/024_drop_food_logs_dedup_backup.sql` — applied.
- `migrations/025_query_audit_catalog_kind.sql` — applied.
- `migrations/026_drop_dead_saas_tables.sql.PROPOSED` — **not** applied (PC sign-off).
- `lib/sql_guard.py` — `audit_query` → `log_query` (new columns, savepoint, catalog+adhoc).
- `lib/views.py` — `run_view` logs every catalog run.
- `monitor/query_log.py` — new maintainer query-observability digest.
- `scripts/dryrun_024_025.py` — dry-run harness.
- Tests: `test_sql_quality.py` (log_query + catalog/adhoc landing), `test_monitor.py`
  (query_log gate).
- Backup: `backups/healthspan-fullbackup-2026-06-04-pre-024-025.{sql,dump}`.
