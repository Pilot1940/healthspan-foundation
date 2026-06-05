-- =============================================================
-- 016c_schema_comments_completion.sql  (ADDITIVE — COMMENT ON only, no data/structure change)
-- Completes the semantic schema map: migration 016 commented 19 high-value tables;
-- 34 live tables (the v3 feature tables, the staging queues, and the catalogs) had
-- ZERO pg_description and were therefore invisible to docs/SCHEMA-MAP.md — an LLM
-- composing ad-hoc SQL could not see them at all. This migration comments every
-- ACTIVE remaining table (MEANING + UNIT + TRAP, same style as 016) and marks the
-- two proven-dead fossils DORMANT rather than documenting them in depth.
--
-- Also SUPERSEDES the 016 comments on supplement_intake_logs (table + taken_on):
-- taken_on is GENERATED ALWAYS from taken_at::date — writing it directly raises
-- Postgres 428C9. The old "Intake date — part of the upsert key" wording misled a
-- hand-written PostgREST insert into supplying it. (Same override pattern 016b used
-- on whoop_cycles.cycle_start.)
--
-- Coverage after this migration: 54/54 public base tables carry a table comment
-- (52 documented + 2 DORMANT); profiles is 12/12 columns.
-- =============================================================
BEGIN;

-- ============ profiles.is_maintainer (added by 022; missed by 016) ============
COMMENT ON COLUMN public.profiles.is_maintainer IS 'TRUE = this profile is a family maintainer (PC). Resolved live via public.is_maintainer() over family_memberships; gates maintainer-only RLS SELECT on query_audit / wearable_sync_log / wearable_sync_errors / stg_*_review. A non-maintainer (Dea/Dev) sees outcomes, never the machinery.';

-- ============ supplement_intake_logs — SUPERSEDE 016 (generated taken_on) ============
COMMENT ON TABLE  public.supplement_intake_logs IS 'Actual supplement intake events. ON CONFLICT key (profile_id, supplement_id, taken_on, source) — taken_on is GENERATED from taken_at, so set taken_at and let it derive; never write taken_on. source CHECK: manual|journal|skill|csv.';
COMMENT ON COLUMN public.supplement_intake_logs.taken_on IS 'GENERATED ALWAYS from taken_at::date — NEVER insert/update (Postgres 428C9). Set taken_at only. Valid as an ON CONFLICT target (part of the upsert key).';

-- =============================================================
-- CATALOGS / REFERENCE DATA (mostly family/global scope, SELECT-only for the skill)
-- =============================================================

-- ============ supplements ============
COMMENT ON TABLE  public.supplements IS 'Catalog of supplement/medication products AND their components (compounds reference their parts via supplement_components). Family/global rows + per-profile custom rows (owner_profile_id). UNIQUE(name).';
COMMENT ON COLUMN public.supplements.id IS 'PK.';
COMMENT ON COLUMN public.supplements.name IS 'Canonical snake_case name (the resolve key; aliases map to it via supplement_aliases). UNIQUE.';
COMMENT ON COLUMN public.supplements.display_name IS 'Human label.';
COMMENT ON COLUMN public.supplements.brand IS 'Brand/manufacturer if relevant.';
COMMENT ON COLUMN public.supplements.key_active IS 'Primary active ingredient (free text).';
COMMENT ON COLUMN public.supplements.category IS 'CHECK: supplement | medication | probiotic | other.';
COMMENT ON COLUMN public.supplements.default_unit IS 'Default dose unit (mg, mcg, g, IU, serving...).';
COMMENT ON COLUMN public.supplements.is_medication IS 'TRUE = prescription/OTC medication (vs nutritional supplement).';
COMMENT ON COLUMN public.supplements.is_compound IS 'TRUE = a multi-ingredient product; its parts are rows linked via supplement_components.';
COMMENT ON COLUMN public.supplements.owner_profile_id IS 'FK profiles.id — set for a per-person CUSTOM entry; NULL = shared/global catalog row.';
COMMENT ON COLUMN public.supplements.is_custom IS 'TRUE = user-added (not curated catalog).';
COMMENT ON COLUMN public.supplements.notes IS 'Free text.';
COMMENT ON COLUMN public.supplements.is_active IS 'Whether the entry is in use.';
COMMENT ON COLUMN public.supplements.created_at IS 'Row insert time.';
COMMENT ON COLUMN public.supplements.updated_at IS 'Last update.';

-- ============ supplement_components ============
COMMENT ON TABLE  public.supplement_components IS 'Composition of a compound supplement: product_id is the parent, component_id an ingredient (both FK supplements.id). CHECK product<>component. UNIQUE(product_id, component_id).';
COMMENT ON COLUMN public.supplement_components.id IS 'PK.';
COMMENT ON COLUMN public.supplement_components.product_id IS 'FK supplements.id — the compound product.';
COMMENT ON COLUMN public.supplement_components.component_id IS 'FK supplements.id — an ingredient of the product.';
COMMENT ON COLUMN public.supplement_components.amount IS 'Amount of the component per serving (numeric).';
COMMENT ON COLUMN public.supplement_components.unit IS 'Unit of amount (mg, mcg, IU...).';
COMMENT ON COLUMN public.supplement_components.notes IS 'Free text.';
COMMENT ON COLUMN public.supplement_components.created_at IS 'Row insert time.';

-- ============ supplement_aliases ============
COMMENT ON TABLE  public.supplement_aliases IS 'Alias → supplements resolution (brand names, abbreviations, OCR variants). UNIQUE(alias, source).';
COMMENT ON COLUMN public.supplement_aliases.id IS 'PK.';
COMMENT ON COLUMN public.supplement_aliases.alias IS 'The alternate string seen in input.';
COMMENT ON COLUMN public.supplement_aliases.supplement_id IS 'FK supplements.id the alias resolves to.';
COMMENT ON COLUMN public.supplement_aliases.source IS 'Where the alias came from (curated, skill, csv...).';
COMMENT ON COLUMN public.supplement_aliases.created_at IS 'Row insert time.';

-- ============ canonical_aliases ============
COMMENT ON TABLE  public.canonical_aliases IS 'Alias → metric_definitions resolution for biomarker-name normalisation (e.g. ag_ratio→albumin_globulin_ratio). UNIQUE(alias, source). Currently empty; resolution presently lives in code — kept pending a keep/drop ruling.';
COMMENT ON COLUMN public.canonical_aliases.id IS 'PK.';
COMMENT ON COLUMN public.canonical_aliases.alias IS 'The non-canonical marker name seen in a lab report.';
COMMENT ON COLUMN public.canonical_aliases.metric_definition_id IS 'FK metric_definitions.id the alias maps to.';
COMMENT ON COLUMN public.canonical_aliases.source IS 'Origin of the mapping.';
COMMENT ON COLUMN public.canonical_aliases.created_at IS 'Row insert time.';

-- ============ metric_definitions catalog companions ============

-- ---- test_definitions ----
COMMENT ON TABLE  public.test_definitions IS 'Catalog of investigation/panel TYPES (e.g. a lipid panel, a DEXA). A healthspan_tests row is one instance of one of these. UNIQUE(name).';
COMMENT ON COLUMN public.test_definitions.id IS 'PK.';
COMMENT ON COLUMN public.test_definitions.name IS 'Canonical snake_case name (UNIQUE).';
COMMENT ON COLUMN public.test_definitions.display_name IS 'Human label.';
COMMENT ON COLUMN public.test_definitions.category IS 'Grouping (blood, imaging, scan, genetic...).';
COMMENT ON COLUMN public.test_definitions.description IS 'What the test covers.';
COMMENT ON COLUMN public.test_definitions.biomarker_count IS 'How many markers this panel typically yields (catalog hint).';
COMMENT ON COLUMN public.test_definitions.is_active IS 'Whether the definition is in use.';
COMMENT ON COLUMN public.test_definitions.created_at IS 'Row insert time.';
COMMENT ON COLUMN public.test_definitions.updated_at IS 'Last update.';

-- ---- test_targets ----
COMMENT ON TABLE  public.test_targets IS 'Reference/optimal ranges per (test_definition, metric_definition), age- and sex-aware. TRAP: these are GENERIC ranges; biomarker_targets holds the PER-PROFILE personal target that overrides them. Currently empty.';
COMMENT ON COLUMN public.test_targets.id IS 'PK.';
COMMENT ON COLUMN public.test_targets.test_definition_id IS 'FK test_definitions.id.';
COMMENT ON COLUMN public.test_targets.metric_definition_id IS 'FK metric_definitions.id.';
COMMENT ON COLUMN public.test_targets.low_normal IS 'Lower bound of the normal/reference range.';
COMMENT ON COLUMN public.test_targets.high_normal IS 'Upper bound of the normal/reference range.';
COMMENT ON COLUMN public.test_targets.low_optimal IS 'Lower bound of the OPTIMAL (tighter than normal) range.';
COMMENT ON COLUMN public.test_targets.high_optimal IS 'Upper bound of the optimal range.';
COMMENT ON COLUMN public.test_targets.age_min IS 'Min age (years) this range applies to. NULL = no lower age bound.';
COMMENT ON COLUMN public.test_targets.age_max IS 'Max age this range applies to. NULL = no upper age bound.';
COMMENT ON COLUMN public.test_targets.sex IS 'CHECK: male | female | all — which sex the range is for.';
COMMENT ON COLUMN public.test_targets.created_at IS 'Row insert time.';
COMMENT ON COLUMN public.test_targets.updated_at IS 'Last update.';

-- ---- biomarker_targets ----
COMMENT ON TABLE  public.biomarker_targets IS 'PER-PROFILE personal target range for a marker (the tighter goal vs the generic test_targets/metric_definitions reference range). Keyed by (profile_id, metric_definition_id, source). Currently empty.';
COMMENT ON COLUMN public.biomarker_targets.id IS 'PK.';
COMMENT ON COLUMN public.biomarker_targets.user_id IS 'LEGACY auth.users id — DORMANT. Use profile_id.';
COMMENT ON COLUMN public.biomarker_targets.metric_definition_id IS 'FK metric_definitions.id — which marker.';
COMMENT ON COLUMN public.biomarker_targets.low_optimal IS 'Personal optimal-range floor.';
COMMENT ON COLUMN public.biomarker_targets.high_optimal IS 'Personal optimal-range ceiling.';
COMMENT ON COLUMN public.biomarker_targets.low_normal IS 'Personal normal-range floor.';
COMMENT ON COLUMN public.biomarker_targets.high_normal IS 'Personal normal-range ceiling.';
COMMENT ON COLUMN public.biomarker_targets.source IS 'Origin of the target (system | clinician | self). Part of the UNIQUE key.';
COMMENT ON COLUMN public.biomarker_targets.notes IS 'Free text.';
COMMENT ON COLUMN public.biomarker_targets.created_at IS 'Row insert time.';
COMMENT ON COLUMN public.biomarker_targets.updated_at IS 'Last update.';
COMMENT ON COLUMN public.biomarker_targets.profile_id IS 'FK profiles.id — the LIVE per-person key.';

-- ---- loinc_reference ----
COMMENT ON TABLE  public.loinc_reference IS 'LOINC code reference for lab markers (metric_definitions.loinc_id → here). Read-only reference data. UNIQUE(loinc_code).';
COMMENT ON COLUMN public.loinc_reference.id IS 'PK.';
COMMENT ON COLUMN public.loinc_reference.loinc_code IS 'LOINC code (UNIQUE).';
COMMENT ON COLUMN public.loinc_reference.component IS 'The substance/analyte measured.';
COMMENT ON COLUMN public.loinc_reference.property IS 'LOINC property axis (e.g. MCnc, SCnc).';
COMMENT ON COLUMN public.loinc_reference.time_aspect IS 'LOINC time axis (Pt, 24H...).';
COMMENT ON COLUMN public.loinc_reference.system IS 'LOINC system/specimen (Ser/Plas, Bld...).';
COMMENT ON COLUMN public.loinc_reference.scale_type IS 'LOINC scale (Qn, Ord, Nom).';
COMMENT ON COLUMN public.loinc_reference.method_type IS 'LOINC method axis if specified.';
COMMENT ON COLUMN public.loinc_reference.long_name IS 'LOINC long common name.';
COMMENT ON COLUMN public.loinc_reference.short_name IS 'LOINC short name.';
COMMENT ON COLUMN public.loinc_reference.unit IS 'Conventional unit for the code.';
COMMENT ON COLUMN public.loinc_reference.created_at IS 'Row insert time.';

-- ============ healthspan_tests ============
COMMENT ON TABLE  public.healthspan_tests IS 'One investigation INSTANCE (a lab visit / DEXA / scan) — groups the biomarkers it produced (biomarkers.healthspan_test_id → here). status CHECK scheduled|pending|complete|partial; investigation_type CHECK blood|imaging|scan|genetic|other.';
COMMENT ON COLUMN public.healthspan_tests.id IS 'PK.';
COMMENT ON COLUMN public.healthspan_tests.user_id IS 'LEGACY auth.users id — DORMANT. Use profile_id.';
COMMENT ON COLUMN public.healthspan_tests.test_definition_id IS 'FK test_definitions.id — which panel type.';
COMMENT ON COLUMN public.healthspan_tests.document_id IS 'FK documents.id (source PDF) if any.';
COMMENT ON COLUMN public.healthspan_tests.tested_at IS 'When the sample/scan was taken (timestamptz, from the report — never now()).';
COMMENT ON COLUMN public.healthspan_tests.lab_name IS 'Lab/provider name.';
COMMENT ON COLUMN public.healthspan_tests.status IS 'CHECK: scheduled | pending | complete | partial.';
COMMENT ON COLUMN public.healthspan_tests.notes IS 'Free text.';
COMMENT ON COLUMN public.healthspan_tests.created_at IS 'Row insert time.';
COMMENT ON COLUMN public.healthspan_tests.updated_at IS 'Last update.';
COMMENT ON COLUMN public.healthspan_tests.profile_id IS 'FK profiles.id — the LIVE per-person key.';
COMMENT ON COLUMN public.healthspan_tests.program_id IS 'FK training_programs.id if the investigation belongs to a program.';
COMMENT ON COLUMN public.healthspan_tests.provider IS 'Provider/clinic (free text).';
COMMENT ON COLUMN public.healthspan_tests.location IS 'Where it was done.';
COMMENT ON COLUMN public.healthspan_tests.investigation_type IS 'CHECK: blood | imaging | scan | genetic | other.';

-- ============ documents ============
COMMENT ON TABLE  public.documents IS 'Uploaded source files (lab PDFs, prescriptions, imaging) that ingestion extracts FROM. Many tables reference document_id back to here. category CHECK lab_report|prescription|imaging|insurance|other; status CHECK uploaded|processing|processed|failed. Currently empty.';
COMMENT ON COLUMN public.documents.id IS 'PK.';
COMMENT ON COLUMN public.documents.user_id IS 'LEGACY auth.users id — DORMANT. Use profile_id.';
COMMENT ON COLUMN public.documents.file_name IS 'Original file name.';
COMMENT ON COLUMN public.documents.file_type IS 'Logical type/extension.';
COMMENT ON COLUMN public.documents.file_size_bytes IS 'Size in bytes.';
COMMENT ON COLUMN public.documents.storage_path IS 'S3 (or storage) path/key.';
COMMENT ON COLUMN public.documents.mime_type IS 'MIME type.';
COMMENT ON COLUMN public.documents.category IS 'CHECK: lab_report | prescription | imaging | insurance | other.';
COMMENT ON COLUMN public.documents.status IS 'CHECK: uploaded | processing | processed | failed.';
COMMENT ON COLUMN public.documents.ai_extracted IS 'TRUE once AI extraction has run on this document.';
COMMENT ON COLUMN public.documents.metadata IS 'JSONB extra metadata.';
COMMENT ON COLUMN public.documents.created_at IS 'Row insert time.';
COMMENT ON COLUMN public.documents.updated_at IS 'Last update.';
COMMENT ON COLUMN public.documents.profile_id IS 'FK profiles.id — the LIVE per-person key.';

-- ============ food_rules ============
COMMENT ON TABLE  public.food_rules IS 'PER-PROFILE dietary rules (allergies, intolerances, preferences, medical avoidances). TRAP: distinct from food_guidance, which is FAMILY/GLOBAL classification. rule_type CHECK allergy|intolerance|preference|medical; severity CHECK mild|moderate|severe|life_threatening. Currently empty.';
COMMENT ON COLUMN public.food_rules.id IS 'PK.';
COMMENT ON COLUMN public.food_rules.user_id IS 'LEGACY auth.users id — DORMANT. Use profile_id.';
COMMENT ON COLUMN public.food_rules.rule_type IS 'CHECK: allergy | intolerance | preference | medical.';
COMMENT ON COLUMN public.food_rules.item IS 'The food/ingredient the rule is about.';
COMMENT ON COLUMN public.food_rules.severity IS 'CHECK: mild | moderate | severe | life_threatening.';
COMMENT ON COLUMN public.food_rules.notes IS 'Free text.';
COMMENT ON COLUMN public.food_rules.source IS 'Origin (manual, skill...).';
COMMENT ON COLUMN public.food_rules.is_active IS 'Whether the rule is in force.';
COMMENT ON COLUMN public.food_rules.created_at IS 'Row insert time.';
COMMENT ON COLUMN public.food_rules.updated_at IS 'Last update.';
COMMENT ON COLUMN public.food_rules.profile_id IS 'FK profiles.id — the LIVE per-person key.';

-- ============ food_guidance ============
COMMENT ON TABLE  public.food_guidance IS 'FAMILY/GLOBAL food classification (the curated avoid/minimize/superfood list, incl. Viome-derived). TRAP: distinct from food_rules (per-profile medical/allergy rules). classification CHECK avoid|minimize|superfood. UNIQUE(item, classification, scope).';
COMMENT ON COLUMN public.food_guidance.id IS 'PK.';
COMMENT ON COLUMN public.food_guidance.item IS 'Food/ingredient name.';
COMMENT ON COLUMN public.food_guidance.classification IS 'CHECK: avoid | minimize | superfood.';
COMMENT ON COLUMN public.food_guidance.reason IS 'Why it is classified this way.';
COMMENT ON COLUMN public.food_guidance.alternative IS 'Suggested swap.';
COMMENT ON COLUMN public.food_guidance.category IS 'Food category grouping.';
COMMENT ON COLUMN public.food_guidance.scope IS 'family (default) or a narrower scope. Part of the UNIQUE key.';
COMMENT ON COLUMN public.food_guidance.source IS 'Origin (curated, viome...).';
COMMENT ON COLUMN public.food_guidance.is_active IS 'Whether the guidance is in use.';
COMMENT ON COLUMN public.food_guidance.created_at IS 'Row insert time.';
COMMENT ON COLUMN public.food_guidance.updated_at IS 'Last update.';

-- ============ weight_logs ============
COMMENT ON TABLE  public.weight_logs IS 'Body-weight log (kg). UNIQUE(profile_id, logged_at). Currently empty — weight currently arrives as a DEXA/lab biomarker; this is the dedicated time-series home.';
COMMENT ON COLUMN public.weight_logs.id IS 'PK.';
COMMENT ON COLUMN public.weight_logs.user_id IS 'LEGACY auth.users id — DORMANT. Use profile_id.';
COMMENT ON COLUMN public.weight_logs.weight_kg IS 'Weight in KILOGRAMS (unit is fixed; do not assume lb).';
COMMENT ON COLUMN public.weight_logs.source IS 'Origin (manual, scale, skill...).';
COMMENT ON COLUMN public.weight_logs.logged_at IS 'When the weight was taken (timestamptz). Part of the UNIQUE key.';
COMMENT ON COLUMN public.weight_logs.notes IS 'Free text.';
COMMENT ON COLUMN public.weight_logs.created_at IS 'Row insert time.';
COMMENT ON COLUMN public.weight_logs.profile_id IS 'FK profiles.id — the LIVE per-person key.';

-- =============================================================
-- WHOOP JOURNAL (habit answers) — source of the whoop_journal pivot VIEW
-- =============================================================

-- ============ whoop_journal_entries ============
COMMENT ON TABLE  public.whoop_journal_entries IS 'Long-form WHOOP journal answers: one row per (cycle, behaviour). The whoop_journal VIEW (016b) pivots these into one boolean/quantity column per habit. UNIQUE(profile_id, cycle_start, behavior_id). JOIN TRAP: cycle_start is CSV-sourced and matches whoop_cycles.cycle_start only on ::date, never the exact timestamp.';
COMMENT ON COLUMN public.whoop_journal_entries.id IS 'PK.';
COMMENT ON COLUMN public.whoop_journal_entries.profile_id IS 'FK profiles.id — the LIVE per-person key.';
COMMENT ON COLUMN public.whoop_journal_entries.cycle_start IS 'Owning WHOOP cycle start (timestamptz, from the CSV). JOIN TRAP: join to whoop_cycles on cycle_start::date, NOT the exact timestamp (exact match returns 0 rows).';
COMMENT ON COLUMN public.whoop_journal_entries.cycle_end IS 'Owning cycle end (timestamptz).';
COMMENT ON COLUMN public.whoop_journal_entries.timezone IS 'WHOOP timezone_offset at cycle time.';
COMMENT ON COLUMN public.whoop_journal_entries.behavior_id IS 'FK journal_behaviors.id — which habit question this answer is for. Part of the upsert key.';
COMMENT ON COLUMN public.whoop_journal_entries.answered_yes IS 'Boolean answer for a yes/no habit (NULL if the behaviour is quantitative or unanswered).';
COMMENT ON COLUMN public.whoop_journal_entries.quantity IS 'Numeric answer for a quantitative behaviour (e.g. glasses of water). NULL for yes/no habits.';
COMMENT ON COLUMN public.whoop_journal_entries.unit IS 'Unit for quantity, if any.';
COMMENT ON COLUMN public.whoop_journal_entries.notes IS 'Free text.';
COMMENT ON COLUMN public.whoop_journal_entries.source_file IS 'Origin CSV/export name.';
COMMENT ON COLUMN public.whoop_journal_entries.created_at IS 'Row insert time.';

-- ============ journal_behaviors ============
COMMENT ON TABLE  public.journal_behaviors IS 'Catalog of WHOOP journal habit QUESTIONS (the columns the whoop_journal view exposes). category CHECK diet|supplements|hydration|alcohol|sleep|wellness|other. UNIQUE(slug).';
COMMENT ON COLUMN public.journal_behaviors.id IS 'PK.';
COMMENT ON COLUMN public.journal_behaviors.slug IS 'Stable snake_case key (becomes the whoop_journal view column; UNIQUE).';
COMMENT ON COLUMN public.journal_behaviors.question_text IS 'The journal prompt as WHOOP phrases it.';
COMMENT ON COLUMN public.journal_behaviors.display_name IS 'Human label.';
COMMENT ON COLUMN public.journal_behaviors.category IS 'CHECK: diet | supplements | hydration | alcohol | sleep | wellness | other.';
COMMENT ON COLUMN public.journal_behaviors.is_quantitative IS 'TRUE = answer is a quantity (quantity col); FALSE = yes/no (answered_yes col).';
COMMENT ON COLUMN public.journal_behaviors.default_unit IS 'Default unit for a quantitative behaviour.';
COMMENT ON COLUMN public.journal_behaviors.is_active IS 'Whether the habit is in use.';
COMMENT ON COLUMN public.journal_behaviors.created_at IS 'Row insert time.';
COMMENT ON COLUMN public.journal_behaviors.updated_at IS 'Last update.';

-- ============ journal_behavior_aliases ============
COMMENT ON TABLE  public.journal_behavior_aliases IS 'Alias → journal_behaviors resolution (WHOOP rewords prompts over time; aliases keep history joinable). UNIQUE(alias).';
COMMENT ON COLUMN public.journal_behavior_aliases.id IS 'PK.';
COMMENT ON COLUMN public.journal_behavior_aliases.alias IS 'An alternate prompt/label string (UNIQUE).';
COMMENT ON COLUMN public.journal_behavior_aliases.behavior_id IS 'FK journal_behaviors.id the alias resolves to.';
COMMENT ON COLUMN public.journal_behavior_aliases.created_at IS 'Row insert time.';

-- =============================================================
-- v3 FEATURE TABLES — goals, programs, alerts, daily logs, audit, tokens
-- =============================================================

-- ============ user_goals ============
COMMENT ON TABLE  public.user_goals IS 'Health/training goals — MULTIPLE concurrent goals per profile allowed (migration 020). TRAP: direction-aware — compare achieved/current vs baseline_value toward target_value; do NOT assume "higher is better". An open goal has is_active=true and ended_at NULL.';
COMMENT ON COLUMN public.user_goals.id IS 'PK.';
COMMENT ON COLUMN public.user_goals.user_id IS 'LEGACY auth.users id — DORMANT. Use profile_id.';
COMMENT ON COLUMN public.user_goals.metric_definition_id IS 'FK metric_definitions.id if the goal tracks a specific marker (NULL for free-form goals).';
COMMENT ON COLUMN public.user_goals.title IS 'Goal title.';
COMMENT ON COLUMN public.user_goals.description IS 'Goal detail.';
COMMENT ON COLUMN public.user_goals.target_value IS 'Numeric target to reach.';
COMMENT ON COLUMN public.user_goals.target_unit IS 'Unit of target_value.';
COMMENT ON COLUMN public.user_goals.target_date IS 'Date by which to hit the target.';
COMMENT ON COLUMN public.user_goals.is_active IS 'TRUE = open/ongoing goal. Derive status with ended_at.';
COMMENT ON COLUMN public.user_goals.progress_pct IS 'Cached progress percent (0-100); recompute from live data when displaying.';
COMMENT ON COLUMN public.user_goals.ended_at IS 'When the goal was closed (NULL = still open).';
COMMENT ON COLUMN public.user_goals.created_at IS 'Row insert time.';
COMMENT ON COLUMN public.user_goals.updated_at IS 'Last update.';
COMMENT ON COLUMN public.user_goals.profile_id IS 'FK profiles.id — the LIVE per-person key.';
COMMENT ON COLUMN public.user_goals.program_id IS 'FK training_programs.id if the goal belongs to a program.';
COMMENT ON COLUMN public.user_goals.baseline_value IS 'Starting value when the goal was set — the reference for direction-aware progress.';
COMMENT ON COLUMN public.user_goals.achieved_value IS 'Best/last value achieved.';
COMMENT ON COLUMN public.user_goals.achieved_date IS 'Date the achieved_value was reached.';

-- ============ training_programs ============
COMMENT ON TABLE  public.training_programs IS 'A training program / plan (e.g. the Mera Peak plan). Has ordered program_phases and program_workouts. status CHECK planned|active|done|abandoned.';
COMMENT ON COLUMN public.training_programs.id IS 'PK.';
COMMENT ON COLUMN public.training_programs.profile_id IS 'FK profiles.id — the LIVE per-person key.';
COMMENT ON COLUMN public.training_programs.name IS 'Program name.';
COMMENT ON COLUMN public.training_programs.objective IS 'What the program is for.';
COMMENT ON COLUMN public.training_programs.target_event IS 'The event/date it builds toward (e.g. a summit).';
COMMENT ON COLUMN public.training_programs.start_date IS 'Program start (date).';
COMMENT ON COLUMN public.training_programs.end_date IS 'Program end (date).';
COMMENT ON COLUMN public.training_programs.status IS 'CHECK: planned | active | done | abandoned.';
COMMENT ON COLUMN public.training_programs.created_at IS 'Row insert time.';
COMMENT ON COLUMN public.training_programs.updated_at IS 'Last update.';

-- ============ program_phases ============
COMMENT ON TABLE  public.program_phases IS 'Ordered phases within a training_program (base / build / peak / taper...). UNIQUE(program_id, ordinal).';
COMMENT ON COLUMN public.program_phases.id IS 'PK.';
COMMENT ON COLUMN public.program_phases.program_id IS 'FK training_programs.id.';
COMMENT ON COLUMN public.program_phases.name IS 'Phase name.';
COMMENT ON COLUMN public.program_phases.ordinal IS 'Order within the program (0-based). UNIQUE with program_id.';
COMMENT ON COLUMN public.program_phases.start_date IS 'Phase start (date).';
COMMENT ON COLUMN public.program_phases.end_date IS 'Phase end (date).';
COMMENT ON COLUMN public.program_phases.weekly_template IS 'JSONB weekly session template for the phase.';
COMMENT ON COLUMN public.program_phases.created_at IS 'Row insert time.';

-- ============ program_workouts ============
COMMENT ON TABLE  public.program_workouts IS 'Links a planned/performed whoop_workouts row to a program (and optionally a phase). UNIQUE(program_id, workout_id). Currently empty.';
COMMENT ON COLUMN public.program_workouts.id IS 'PK.';
COMMENT ON COLUMN public.program_workouts.program_id IS 'FK training_programs.id.';
COMMENT ON COLUMN public.program_workouts.phase_id IS 'FK program_phases.id (NULL if not phase-assigned).';
COMMENT ON COLUMN public.program_workouts.workout_id IS 'FK whoop_workouts.id.';
COMMENT ON COLUMN public.program_workouts.workout_type IS 'Planned type/label for the session.';
COMMENT ON COLUMN public.program_workouts.prescribed IS 'TRUE = prescribed by the plan (vs a workout retro-tagged to it).';
COMMENT ON COLUMN public.program_workouts.notes IS 'Free text.';
COMMENT ON COLUMN public.program_workouts.created_at IS 'Row insert time.';

-- ============ trend_alerts ============
COMMENT ON TABLE  public.trend_alerts IS 'Generated alerts (threshold breach, trend, anomaly, goal). alert_type CHECK threshold|trend|anomaly|goal; severity CHECK info|warning|critical. Currently empty (alerting not yet wired).';
COMMENT ON COLUMN public.trend_alerts.id IS 'PK.';
COMMENT ON COLUMN public.trend_alerts.user_id IS 'LEGACY auth.users id — DORMANT. Use profile_id.';
COMMENT ON COLUMN public.trend_alerts.metric_definition_id IS 'FK metric_definitions.id if the alert is about a marker (NULL otherwise).';
COMMENT ON COLUMN public.trend_alerts.alert_type IS 'CHECK: threshold | trend | anomaly | goal.';
COMMENT ON COLUMN public.trend_alerts.severity IS 'CHECK: info | warning | critical.';
COMMENT ON COLUMN public.trend_alerts.title IS 'Short alert title.';
COMMENT ON COLUMN public.trend_alerts.message IS 'Alert body.';
COMMENT ON COLUMN public.trend_alerts.data IS 'JSONB supporting data/snapshot.';
COMMENT ON COLUMN public.trend_alerts.is_read IS 'Whether the alert has been read.';
COMMENT ON COLUMN public.trend_alerts.read_at IS 'When it was read.';
COMMENT ON COLUMN public.trend_alerts.created_at IS 'Row insert time.';
COMMENT ON COLUMN public.trend_alerts.profile_id IS 'FK profiles.id — the LIVE per-person key.';

-- ============ daily_logs ============
COMMENT ON TABLE  public.daily_logs IS 'Generic per-day log header (one per profile per log_date/log_type); its metric values hang off daily_log_metrics. Currently empty — most signals land in their typed tables (food_logs, biomarkers, whoop_*). Generic catch-all.';
COMMENT ON COLUMN public.daily_logs.id IS 'PK.';
COMMENT ON COLUMN public.daily_logs.user_id IS 'LEGACY auth.users id — DORMANT. Use profile_id.';
COMMENT ON COLUMN public.daily_logs.log_date IS 'The day this log covers (date).';
COMMENT ON COLUMN public.daily_logs.log_type IS 'Kind of daily log (default ''general'').';
COMMENT ON COLUMN public.daily_logs.source IS 'Origin (manual, skill...).';
COMMENT ON COLUMN public.daily_logs.notes IS 'Free text.';
COMMENT ON COLUMN public.daily_logs.created_at IS 'Row insert time.';
COMMENT ON COLUMN public.daily_logs.updated_at IS 'Last update.';
COMMENT ON COLUMN public.daily_logs.profile_id IS 'FK profiles.id — the LIVE per-person key.';

-- ============ daily_log_metrics ============
COMMENT ON TABLE  public.daily_log_metrics IS 'Metric values attached to a daily_logs row (EAV-style). Currently empty. Use the typed tables (biomarkers etc.) for real analysis.';
COMMENT ON COLUMN public.daily_log_metrics.id IS 'PK.';
COMMENT ON COLUMN public.daily_log_metrics.user_id IS 'LEGACY auth.users id — DORMANT. Use profile_id.';
COMMENT ON COLUMN public.daily_log_metrics.daily_log_id IS 'FK daily_logs.id (CASCADE).';
COMMENT ON COLUMN public.daily_log_metrics.metric_definition_id IS 'FK metric_definitions.id — which metric.';
COMMENT ON COLUMN public.daily_log_metrics.value IS 'Numeric value.';
COMMENT ON COLUMN public.daily_log_metrics.unit IS 'Unit of value.';
COMMENT ON COLUMN public.daily_log_metrics.created_at IS 'Row insert time.';
COMMENT ON COLUMN public.daily_log_metrics.profile_id IS 'FK profiles.id — the LIVE per-person key.';

-- ============ query_audit (migration 025) ============
COMMENT ON TABLE  public.query_audit IS 'Skill read-audit log: one row per skill query (lib.views.run_view = catalog, lib.sql_guard.run_adhoc_audited = adhoc). MAINTAINER-ONLY RLS SELECT (022) — non-maintainers see 0 rows. kind CHECK catalog|adhoc. Written non-blocking via lib.sql_guard.log_query.';
COMMENT ON COLUMN public.query_audit.id IS 'PK.';
COMMENT ON COLUMN public.query_audit.profile_id IS 'FK profiles.id — whose data the query ran against.';
COMMENT ON COLUMN public.query_audit.ts IS 'When the query ran (timestamptz).';
COMMENT ON COLUMN public.query_audit.quality_verdict IS 'JSONB data-quality verdict for the result (e.g. nullable-aggregate warning).';
COMMENT ON COLUMN public.query_audit.row_count IS 'Rows the query returned.';
COMMENT ON COLUMN public.query_audit.flagged IS 'TRUE = flagged for maintainer review.';
COMMENT ON COLUMN public.query_audit.kind IS 'CHECK: catalog (a named lib/views entry) | adhoc (a composed SQL).';
COMMENT ON COLUMN public.query_audit.name_or_sql IS 'The catalog view name (kind=catalog) or the raw SQL text (kind=adhoc).';
COMMENT ON COLUMN public.query_audit.params IS 'JSONB bound parameters / intent.';

-- ============ whoop_tokens (migration 015) ============
COMMENT ON TABLE  public.whoop_tokens IS 'WHOOP OAuth tokens per profile, used by the WHOOP API sync (ingest/whoop_sync). UNIQUE(profile_id). SECRET — access/refresh tokens; never surface in skill output.';
COMMENT ON COLUMN public.whoop_tokens.id IS 'PK.';
COMMENT ON COLUMN public.whoop_tokens.profile_id IS 'FK profiles.id — whose WHOOP account this is (UNIQUE).';
COMMENT ON COLUMN public.whoop_tokens.access_token IS 'OAuth access token (SECRET).';
COMMENT ON COLUMN public.whoop_tokens.refresh_token IS 'OAuth refresh token (SECRET).';
COMMENT ON COLUMN public.whoop_tokens.expires_at IS 'Access-token expiry (refresh when past).';
COMMENT ON COLUMN public.whoop_tokens.scope IS 'Granted OAuth scopes.';
COMMENT ON COLUMN public.whoop_tokens.whoop_user_id IS 'WHOOP account user id.';
COMMENT ON COLUMN public.whoop_tokens.updated_at IS 'Last token refresh time.';

-- ============ audit_log ============
COMMENT ON TABLE  public.audit_log IS 'Generic row-level change audit (action/table/old_data/new_data). Currently EMPTY and not written by the v3 skill — kept (migration 028) pending a keep/drop ruling. Do not rely on it for history.';
COMMENT ON COLUMN public.audit_log.id IS 'PK.';
COMMENT ON COLUMN public.audit_log.user_id IS 'auth.users id who performed the action.';
COMMENT ON COLUMN public.audit_log.action IS 'INSERT | UPDATE | DELETE (free text).';
COMMENT ON COLUMN public.audit_log.table_name IS 'Table the change was on.';
COMMENT ON COLUMN public.audit_log.record_id IS 'PK of the changed row.';
COMMENT ON COLUMN public.audit_log.old_data IS 'JSONB row before.';
COMMENT ON COLUMN public.audit_log.new_data IS 'JSONB row after.';
COMMENT ON COLUMN public.audit_log.ip_address IS 'Client IP (inet).';
COMMENT ON COLUMN public.audit_log.user_agent IS 'Client user agent.';
COMMENT ON COLUMN public.audit_log.created_at IS 'When the action happened.';

-- =============================================================
-- STAGING REVIEW QUEUES — AI extraction lands here FIRST (CLAUDE.md rule #2),
-- never directly in prod. confidence 0-1; status CHECK pending|approved|rejected|merged.
-- TRAP: NOT production truth — a pending/rejected row is unverified extraction.
-- =============================================================

-- ============ stg_biomarker_review ============
COMMENT ON TABLE  public.stg_biomarker_review IS 'Staging queue for AI-extracted biomarkers (rule #2: extraction → staging, never prod). On approval, promotes to biomarkers.staging_id. MAINTAINER-gated. TRAP: not production truth. status CHECK pending|approved|rejected|merged; confidence 0-1.';
COMMENT ON COLUMN public.stg_biomarker_review.id IS 'PK.';
COMMENT ON COLUMN public.stg_biomarker_review.user_id IS 'LEGACY auth.users id — DORMANT. Use profile_id.';
COMMENT ON COLUMN public.stg_biomarker_review.document_id IS 'FK documents.id source PDF.';
COMMENT ON COLUMN public.stg_biomarker_review.metric_definition_id IS 'FK metric_definitions.id once resolved (NULL = unresolved name to review).';
COMMENT ON COLUMN public.stg_biomarker_review.extracted_name IS 'Raw marker name as read from the source.';
COMMENT ON COLUMN public.stg_biomarker_review.extracted_value IS 'Raw value extracted.';
COMMENT ON COLUMN public.stg_biomarker_review.extracted_unit IS 'Raw unit extracted.';
COMMENT ON COLUMN public.stg_biomarker_review.measured_at IS 'Sample date as read from the report.';
COMMENT ON COLUMN public.stg_biomarker_review.confidence IS 'Extraction confidence 0-1 (CHECK). Below the system_config cutoff → stays for review.';
COMMENT ON COLUMN public.stg_biomarker_review.status IS 'CHECK: pending | approved | rejected | merged.';
COMMENT ON COLUMN public.stg_biomarker_review.reviewed_at IS 'When a maintainer actioned it.';
COMMENT ON COLUMN public.stg_biomarker_review.raw_text IS 'The source text snippet the value came from.';
COMMENT ON COLUMN public.stg_biomarker_review.created_at IS 'Row insert time.';
COMMENT ON COLUMN public.stg_biomarker_review.profile_id IS 'FK profiles.id — the LIVE per-person key.';

-- ============ stg_food_log_review ============
COMMENT ON TABLE  public.stg_food_log_review IS 'Staging queue for AI-extracted food logs (rule #2). On approval, promotes to food_logs. TRAP: not production truth. status CHECK pending|approved|rejected|merged; confidence 0-1.';
COMMENT ON COLUMN public.stg_food_log_review.id IS 'PK.';
COMMENT ON COLUMN public.stg_food_log_review.profile_id IS 'FK profiles.id — the LIVE per-person key.';
COMMENT ON COLUMN public.stg_food_log_review.user_id IS 'LEGACY auth.users id — DORMANT. Use profile_id.';
COMMENT ON COLUMN public.stg_food_log_review.document_id IS 'FK documents.id source.';
COMMENT ON COLUMN public.stg_food_log_review.sync_log_id IS 'FK wearable_sync_log.id (the ingest run that produced this).';
COMMENT ON COLUMN public.stg_food_log_review.meal_type IS 'Extracted meal type.';
COMMENT ON COLUMN public.stg_food_log_review.description IS 'Extracted meal description.';
COMMENT ON COLUMN public.stg_food_log_review.calories IS 'Extracted kcal (thousands separators stripped — see food_logs.calories trap).';
COMMENT ON COLUMN public.stg_food_log_review.protein_g IS 'Extracted protein (g).';
COMMENT ON COLUMN public.stg_food_log_review.carbs_g IS 'Extracted carbohydrate (g).';
COMMENT ON COLUMN public.stg_food_log_review.fat_g IS 'Extracted fat (g).';
COMMENT ON COLUMN public.stg_food_log_review.fiber_g IS 'Extracted fibre (g).';
COMMENT ON COLUMN public.stg_food_log_review.foods IS 'JSONB array of extracted food items.';
COMMENT ON COLUMN public.stg_food_log_review.verdict IS 'Extracted diet-fit verdict.';
COMMENT ON COLUMN public.stg_food_log_review.confidence IS 'Extraction confidence 0-1 (CHECK).';
COMMENT ON COLUMN public.stg_food_log_review.status IS 'CHECK: pending | approved | rejected | merged.';
COMMENT ON COLUMN public.stg_food_log_review.reviewed_at IS 'When actioned.';
COMMENT ON COLUMN public.stg_food_log_review.raw_text IS 'Source text snippet.';
COMMENT ON COLUMN public.stg_food_log_review.created_at IS 'Row insert time.';

-- ============ stg_food_rule_review ============
COMMENT ON TABLE  public.stg_food_rule_review IS 'Staging queue for AI-extracted food_rules (rule #2). TRAP: not production truth. status CHECK pending|approved|rejected|merged; confidence 0-1.';
COMMENT ON COLUMN public.stg_food_rule_review.id IS 'PK.';
COMMENT ON COLUMN public.stg_food_rule_review.user_id IS 'LEGACY auth.users id — DORMANT. Use profile_id.';
COMMENT ON COLUMN public.stg_food_rule_review.document_id IS 'FK documents.id source.';
COMMENT ON COLUMN public.stg_food_rule_review.rule_type IS 'Extracted rule type (see food_rules.rule_type CHECK on promotion).';
COMMENT ON COLUMN public.stg_food_rule_review.item IS 'Extracted food/ingredient.';
COMMENT ON COLUMN public.stg_food_rule_review.severity IS 'Extracted severity.';
COMMENT ON COLUMN public.stg_food_rule_review.confidence IS 'Extraction confidence 0-1 (CHECK).';
COMMENT ON COLUMN public.stg_food_rule_review.status IS 'CHECK: pending | approved | rejected | merged.';
COMMENT ON COLUMN public.stg_food_rule_review.reviewed_at IS 'When actioned.';
COMMENT ON COLUMN public.stg_food_rule_review.raw_text IS 'Source text snippet.';
COMMENT ON COLUMN public.stg_food_rule_review.created_at IS 'Row insert time.';
COMMENT ON COLUMN public.stg_food_rule_review.profile_id IS 'FK profiles.id — the LIVE per-person key.';

-- ============ stg_supplement_intake_review ============
COMMENT ON TABLE  public.stg_supplement_intake_review IS 'Staging queue for AI-extracted supplement intake events (rule #2). On approval, promotes to supplement_intake_logs. TRAP: not production truth. status CHECK pending|approved|rejected|merged; confidence 0-1.';
COMMENT ON COLUMN public.stg_supplement_intake_review.id IS 'PK.';
COMMENT ON COLUMN public.stg_supplement_intake_review.profile_id IS 'FK profiles.id — the LIVE per-person key.';
COMMENT ON COLUMN public.stg_supplement_intake_review.document_id IS 'FK documents.id source.';
COMMENT ON COLUMN public.stg_supplement_intake_review.sync_log_id IS 'FK wearable_sync_log.id ingest run.';
COMMENT ON COLUMN public.stg_supplement_intake_review.supplement_id IS 'FK supplements.id once resolved (NULL = unresolved name to review).';
COMMENT ON COLUMN public.stg_supplement_intake_review.extracted_name IS 'Raw supplement name as read.';
COMMENT ON COLUMN public.stg_supplement_intake_review.extracted_dose IS 'Raw dose amount.';
COMMENT ON COLUMN public.stg_supplement_intake_review.extracted_unit IS 'Raw dose unit.';
COMMENT ON COLUMN public.stg_supplement_intake_review.taken_at IS 'Extracted intake timestamp (promotes to supplement_intake_logs.taken_at; taken_on derives).';
COMMENT ON COLUMN public.stg_supplement_intake_review.confidence IS 'Extraction confidence 0-1 (CHECK).';
COMMENT ON COLUMN public.stg_supplement_intake_review.status IS 'CHECK: pending | approved | rejected | merged.';
COMMENT ON COLUMN public.stg_supplement_intake_review.reviewed_at IS 'When actioned.';
COMMENT ON COLUMN public.stg_supplement_intake_review.raw_text IS 'Source text snippet.';
COMMENT ON COLUMN public.stg_supplement_intake_review.created_at IS 'Row insert time.';

-- ============ stg_test_result_review ============
COMMENT ON TABLE  public.stg_test_result_review IS 'Staging queue for AI-extracted test/investigation results (rule #2). TRAP: not production truth. result_value is TEXT (free-form, pre-normalisation). status CHECK pending|approved|rejected|merged; confidence 0-1.';
COMMENT ON COLUMN public.stg_test_result_review.id IS 'PK.';
COMMENT ON COLUMN public.stg_test_result_review.user_id IS 'LEGACY auth.users id — DORMANT. Use profile_id.';
COMMENT ON COLUMN public.stg_test_result_review.document_id IS 'FK documents.id source.';
COMMENT ON COLUMN public.stg_test_result_review.test_name IS 'Raw test name as read.';
COMMENT ON COLUMN public.stg_test_result_review.result_value IS 'Raw result as TEXT (not yet numeric/normalised).';
COMMENT ON COLUMN public.stg_test_result_review.result_unit IS 'Raw unit.';
COMMENT ON COLUMN public.stg_test_result_review.tested_at IS 'Extracted test date.';
COMMENT ON COLUMN public.stg_test_result_review.lab_name IS 'Extracted lab name.';
COMMENT ON COLUMN public.stg_test_result_review.confidence IS 'Extraction confidence 0-1 (CHECK).';
COMMENT ON COLUMN public.stg_test_result_review.status IS 'CHECK: pending | approved | rejected | merged.';
COMMENT ON COLUMN public.stg_test_result_review.reviewed_at IS 'When actioned.';
COMMENT ON COLUMN public.stg_test_result_review.raw_text IS 'Source text snippet.';
COMMENT ON COLUMN public.stg_test_result_review.created_at IS 'Row insert time.';
COMMENT ON COLUMN public.stg_test_result_review.profile_id IS 'FK profiles.id — the LIVE per-person key.';

-- =============================================================
-- INFRASTRUCTURE / CONFIG (active, but not per-person health data)
-- =============================================================

-- ============ system_config ============
COMMENT ON TABLE  public.system_config IS 'Thresholds & runtime config — the SINGLE source for thresholds (CLAUDE.md rule #1: never hardcode). lib.contract.get_config reads value (jsonb) by key. UNIQUE(key). e.g. the ingestion confidence cutoff lives here.';
COMMENT ON COLUMN public.system_config.id IS 'PK.';
COMMENT ON COLUMN public.system_config.key IS 'Config key (UNIQUE) — the lookup string.';
COMMENT ON COLUMN public.system_config.value IS 'JSONB value (parsed to native Python by get_config).';
COMMENT ON COLUMN public.system_config.description IS 'What this config controls.';
COMMENT ON COLUMN public.system_config.category IS 'Grouping for the config entry.';
COMMENT ON COLUMN public.system_config.is_active IS 'TRUE = honoured (get_config filters on is_active).';
COMMENT ON COLUMN public.system_config.created_at IS 'Row insert time.';
COMMENT ON COLUMN public.system_config.updated_at IS 'Last update.';

-- ============ ingestion_tokens ============
COMMENT ON TABLE  public.ingestion_tokens IS 'Hashed ingestion-auth tokens minted by mint_ingestion_token() (hs_ops mint-token) for the photo/screenshot ingestion path. token_hash is a one-way hash (the plaintext is shown ONCE). The analytics/read skill NEVER queries this — it is ingestion-auth infrastructure. UNIQUE(token_hash).';
COMMENT ON COLUMN public.ingestion_tokens.id IS 'PK.';
COMMENT ON COLUMN public.ingestion_tokens.profile_id IS 'FK profiles.id the token ingests for (CASCADE).';
COMMENT ON COLUMN public.ingestion_tokens.token_hash IS 'One-way hash of the token (bytea). The plaintext is never stored. UNIQUE.';
COMMENT ON COLUMN public.ingestion_tokens.label IS 'Human label for the token.';
COMMENT ON COLUMN public.ingestion_tokens.allowed_methods IS 'TEXT[] of permitted ingest methods (default {photo,screenshot}).';
COMMENT ON COLUMN public.ingestion_tokens.created_by IS 'FK auth.users who minted it.';
COMMENT ON COLUMN public.ingestion_tokens.expires_at IS 'Expiry (NULL = no expiry).';
COMMENT ON COLUMN public.ingestion_tokens.revoked_at IS 'When revoked (NULL = active).';
COMMENT ON COLUMN public.ingestion_tokens.last_used_at IS 'Last time the token authenticated an ingest.';
COMMENT ON COLUMN public.ingestion_tokens.created_at IS 'Row insert time.';

-- =============================================================
-- DORMANT — fossils with ZERO code references; superseded. Documented as
-- DORMANT (not column-by-column) per the completion brief. DO NOT QUERY.
-- =============================================================

COMMENT ON TABLE public.users_extended IS 'DORMANT — not used by the skill; do not query. SaaS-era per-user profile fossil (tier free/pro/premium, onboarding_complete). Superseded by public.profiles. Zero code references; kept only as historical structure.';
COMMENT ON TABLE public.body_metrics_history IS 'DORMANT — not used by the skill; do not query. Superseded by public.biomarkers, which is the canonical home for all lab + DEXA scalar values (incl. body composition). Zero rows, zero code references.';

COMMIT;
