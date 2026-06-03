-- =============================================================
-- 016_schema_comments.sql  (ADDITIVE — COMMENT ON only, no data/structure change)
-- PROMPT 16. Semantic schema map encoded as column/table COMMENTs so an LLM
-- composing ad-hoc SQL cannot misread conventions. Comments are the SINGLE source;
-- docs/SCHEMA-MAP.md is generated from pg_description (never hand-edited).
-- Each column comment = MEANING + UNIT + TRAP (where one exists).
-- The high-value traps (NULL recovery, cycle-day offset, is_nap, screenshot
-- approximate, qualifier, Rule #7 never-recompute, kg/mg-L units, canonical names)
-- are encoded verbatim from HealthSpan-Skill-v2-Decisions §B + migration headers.
-- =============================================================
BEGIN;

-- ============ whoop_cycles ============
COMMENT ON TABLE  public.whoop_cycles IS 'WHOOP physiological day (wake-to-wake): one row per cycle. Recovery/HRV/RHR derive from that night''s sleep. Keyed on (profile_id, whoop_id).';
COMMENT ON COLUMN public.whoop_cycles.id IS 'PK (gen_random_uuid).';
COMMENT ON COLUMN public.whoop_cycles.user_id IS 'LEGACY auth.users id — DORMANT. Use profile_id as the live key.';
COMMENT ON COLUMN public.whoop_cycles.cycle_start IS 'Start of the WHOOP physiological day (timestamptz, UTC). TRAP: this is the WHOOP cycle boundary = screenshot date MINUS 1 day; not the calendar date.';
COMMENT ON COLUMN public.whoop_cycles.cycle_end IS 'End of the WHOOP cycle (timestamptz).';
COMMENT ON COLUMN public.whoop_cycles.timezone IS 'WHOOP timezone_offset string (e.g. +05:30) at cycle time.';
COMMENT ON COLUMN public.whoop_cycles.recovery_score_pct IS 'Recovery score 0-100. TRAP: NULL = NO SLEEP that cycle (strap off / nap-only) — EXCLUDE from averages/trends, NEVER treat as 0.';
COMMENT ON COLUMN public.whoop_cycles.resting_hr_bpm IS 'Resting heart rate (bpm). TRAP: NULL = no sleep that cycle; exclude, never average as 0.';
COMMENT ON COLUMN public.whoop_cycles.hrv_ms IS 'HRV RMSSD (ms). TRAP: NULL = no sleep that cycle; exclude, never average as 0.';
COMMENT ON COLUMN public.whoop_cycles.skin_temp_celsius IS 'Skin temperature (°C) from recovery.';
COMMENT ON COLUMN public.whoop_cycles.blood_oxygen_pct IS 'SpO2 (%) from recovery.';
COMMENT ON COLUMN public.whoop_cycles.respiratory_rate_rpm IS 'Respiratory rate (breaths/min) from sleep.';
COMMENT ON COLUMN public.whoop_cycles.day_strain IS 'WHOOP day strain 0-21 (logarithmic). TRAP: strain present + recovery NULL = strap worn in day but not overnight (wear-compliance gap, not health regression).';
COMMENT ON COLUMN public.whoop_cycles.energy_burned_cal IS 'Energy burned for the cycle (kcal; converted from WHOOP kilojoule).';
COMMENT ON COLUMN public.whoop_cycles.max_hr_bpm IS 'Max HR during the cycle (bpm).';
COMMENT ON COLUMN public.whoop_cycles.avg_hr_bpm IS 'Average HR during the cycle (bpm).';
COMMENT ON COLUMN public.whoop_cycles.sleep_onset IS 'Main sleep onset for this cycle (timestamptz).';
COMMENT ON COLUMN public.whoop_cycles.wake_onset IS 'Wake time for this cycle (timestamptz).';
COMMENT ON COLUMN public.whoop_cycles.sleep_performance_pct IS 'Sleep performance % (asleep vs needed).';
COMMENT ON COLUMN public.whoop_cycles.asleep_duration_min IS 'Total asleep time (minutes).';
COMMENT ON COLUMN public.whoop_cycles.in_bed_duration_min IS 'Time in bed (minutes).';
COMMENT ON COLUMN public.whoop_cycles.light_sleep_min IS 'Light sleep (minutes).';
COMMENT ON COLUMN public.whoop_cycles.deep_sws_min IS 'Deep / slow-wave sleep (minutes).';
COMMENT ON COLUMN public.whoop_cycles.rem_min IS 'REM sleep (minutes).';
COMMENT ON COLUMN public.whoop_cycles.awake_min IS 'Awake time during sleep (minutes).';
COMMENT ON COLUMN public.whoop_cycles.sleep_need_min IS 'WHOOP-calculated sleep need (minutes).';
COMMENT ON COLUMN public.whoop_cycles.sleep_debt_min IS 'Sleep debt (minutes).';
COMMENT ON COLUMN public.whoop_cycles.sleep_efficiency_pct IS 'Sleep efficiency %.';
COMMENT ON COLUMN public.whoop_cycles.sleep_consistency_pct IS 'Sleep consistency %.';
COMMENT ON COLUMN public.whoop_cycles.sprint_id IS 'FK sprints.id — auto-tagged by date-overlap trigger (training block this cycle falls in).';
COMMENT ON COLUMN public.whoop_cycles.source_file IS 'Origin: whoop_api | whoop_webhook | screenshot | CSV export name.';
COMMENT ON COLUMN public.whoop_cycles.created_at IS 'Row insert time (not the cycle date).';
COMMENT ON COLUMN public.whoop_cycles.profile_id IS 'FK profiles.id — the LIVE per-person key (RLS scopes on this).';
COMMENT ON COLUMN public.whoop_cycles.whoop_id IS 'WHOOP API cycle UUID — the stable upsert key (profile_id, whoop_id). Re-scores overwrite, never duplicate.';

-- ============ whoop_sleeps ============
COMMENT ON TABLE  public.whoop_sleeps IS 'Individual sleep events incl. naps. Keyed on (profile_id, whoop_id). Most analyses filter is_nap=false.';
COMMENT ON COLUMN public.whoop_sleeps.id IS 'PK.';
COMMENT ON COLUMN public.whoop_sleeps.user_id IS 'LEGACY auth.users id — DORMANT. Use profile_id.';
COMMENT ON COLUMN public.whoop_sleeps.cycle_start IS 'Owning cycle start (timestamptz); links a sleep to its WHOOP cycle.';
COMMENT ON COLUMN public.whoop_sleeps.cycle_end IS 'Owning cycle end (timestamptz).';
COMMENT ON COLUMN public.whoop_sleeps.timezone IS 'WHOOP timezone_offset at sleep time.';
COMMENT ON COLUMN public.whoop_sleeps.sleep_onset IS 'Sleep start (timestamptz).';
COMMENT ON COLUMN public.whoop_sleeps.wake_onset IS 'Wake time (timestamptz).';
COMMENT ON COLUMN public.whoop_sleeps.sleep_performance_pct IS 'Sleep performance %.';
COMMENT ON COLUMN public.whoop_sleeps.respiratory_rate_rpm IS 'Respiratory rate (breaths/min).';
COMMENT ON COLUMN public.whoop_sleeps.asleep_duration_min IS 'Asleep time (minutes).';
COMMENT ON COLUMN public.whoop_sleeps.in_bed_duration_min IS 'Time in bed (minutes).';
COMMENT ON COLUMN public.whoop_sleeps.light_sleep_min IS 'Light sleep (minutes).';
COMMENT ON COLUMN public.whoop_sleeps.deep_sws_min IS 'Deep / slow-wave sleep (minutes).';
COMMENT ON COLUMN public.whoop_sleeps.rem_min IS 'REM sleep (minutes).';
COMMENT ON COLUMN public.whoop_sleeps.awake_min IS 'Awake time (minutes).';
COMMENT ON COLUMN public.whoop_sleeps.sleep_need_min IS 'Sleep need (minutes).';
COMMENT ON COLUMN public.whoop_sleeps.sleep_debt_min IS 'Sleep debt (minutes).';
COMMENT ON COLUMN public.whoop_sleeps.sleep_efficiency_pct IS 'Sleep efficiency %.';
COMMENT ON COLUMN public.whoop_sleeps.sleep_consistency_pct IS 'Sleep consistency %.';
COMMENT ON COLUMN public.whoop_sleeps.is_nap IS 'TRAP: TRUE = nap, separate from the main overnight sleep. Most sleep analyses filter is_nap=false.';
COMMENT ON COLUMN public.whoop_sleeps.sprint_id IS 'FK sprints.id (auto-tagged).';
COMMENT ON COLUMN public.whoop_sleeps.source_file IS 'Origin (whoop_api/webhook/screenshot/CSV).';
COMMENT ON COLUMN public.whoop_sleeps.created_at IS 'Row insert time.';
COMMENT ON COLUMN public.whoop_sleeps.profile_id IS 'FK profiles.id — live per-person key.';
COMMENT ON COLUMN public.whoop_sleeps.whoop_id IS 'WHOOP API sleep UUID — stable upsert key (profile_id, whoop_id).';

-- ============ whoop_workouts ============
COMMENT ON TABLE  public.whoop_workouts IS 'Workouts with zone durations. Keyed on (profile_id, whoop_id). Zone seconds (hr_zoneN_sec) are exact from API; percentages are derived.';
COMMENT ON COLUMN public.whoop_workouts.id IS 'PK.';
COMMENT ON COLUMN public.whoop_workouts.user_id IS 'LEGACY auth.users id — DORMANT. Use profile_id.';
COMMENT ON COLUMN public.whoop_workouts.cycle_start IS 'Owning WHOOP cycle start (timestamptz).';
COMMENT ON COLUMN public.whoop_workouts.cycle_end IS 'Owning cycle end.';
COMMENT ON COLUMN public.whoop_workouts.timezone IS 'WHOOP timezone_offset at workout time.';
COMMENT ON COLUMN public.whoop_workouts.workout_start IS 'Workout start (timestamptz, UTC). Render in timezone for local wall-clock.';
COMMENT ON COLUMN public.whoop_workouts.workout_end IS 'Workout end (timestamptz).';
COMMENT ON COLUMN public.whoop_workouts.duration_min IS 'Duration (minutes).';
COMMENT ON COLUMN public.whoop_workouts.activity_name IS 'Sport/activity name (WHOOP sport_name, e.g. functional-fitness, running, walking).';
COMMENT ON COLUMN public.whoop_workouts.activity_strain IS 'Workout strain 0-21 (logarithmic).';
COMMENT ON COLUMN public.whoop_workouts.energy_burned_cal IS 'Energy burned (kcal; from kilojoule).';
COMMENT ON COLUMN public.whoop_workouts.max_hr_bpm IS 'Max HR (bpm).';
COMMENT ON COLUMN public.whoop_workouts.avg_hr_bpm IS 'Average HR (bpm).';
COMMENT ON COLUMN public.whoop_workouts.hr_zone1_pct IS 'Zone 1 % of workout (derived). Zone *_sec are the precise source.';
COMMENT ON COLUMN public.whoop_workouts.hr_zone2_pct IS 'Zone 2 % (derived).';
COMMENT ON COLUMN public.whoop_workouts.hr_zone3_pct IS 'Zone 3 % (derived).';
COMMENT ON COLUMN public.whoop_workouts.hr_zone4_pct IS 'Zone 4 % (derived). Z4=threshold (90-95% max in 4x4 protocols).';
COMMENT ON COLUMN public.whoop_workouts.hr_zone5_pct IS 'Zone 5 % (derived). Z5=>95% max; high Z5 share vs Z4 = redlining.';
COMMENT ON COLUMN public.whoop_workouts.gps_enabled IS 'Whether GPS was on.';
COMMENT ON COLUMN public.whoop_workouts.sprint_id IS 'FK sprints.id (auto-tagged).';
COMMENT ON COLUMN public.whoop_workouts.source_file IS 'Origin (whoop_api/webhook/screenshot/CSV).';
COMMENT ON COLUMN public.whoop_workouts.created_at IS 'Row insert time.';
COMMENT ON COLUMN public.whoop_workouts.profile_id IS 'FK profiles.id — live per-person key.';
COMMENT ON COLUMN public.whoop_workouts.tags IS 'JSONB array of program/label tags. Coach-note from screenshots stored here as {coach_note,...}.';
COMMENT ON COLUMN public.whoop_workouts.hr_zone0_pct IS 'Zone 0 (<110 bpm) % (derived).';
COMMENT ON COLUMN public.whoop_workouts.hr_zone0_sec IS 'Zone 0 seconds (<110 bpm). From API zone_durations.';
COMMENT ON COLUMN public.whoop_workouts.hr_zone1_sec IS 'Zone 1 seconds (recovery). Precise from API.';
COMMENT ON COLUMN public.whoop_workouts.hr_zone2_sec IS 'Zone 2 seconds (aerobic base). Precise from API.';
COMMENT ON COLUMN public.whoop_workouts.hr_zone3_sec IS 'Zone 3 seconds (tempo / transition). Precise from API.';
COMMENT ON COLUMN public.whoop_workouts.hr_zone4_sec IS 'Zone 4 seconds (threshold, 90-95% max). Precise from API.';
COMMENT ON COLUMN public.whoop_workouts.hr_zone5_sec IS 'Zone 5 seconds (>95% max). Precise from API.';
COMMENT ON COLUMN public.whoop_workouts.cardio_load_pct IS 'Cardio load split %. NULL from API (app-only / screenshot).';
COMMENT ON COLUMN public.whoop_workouts.muscular_load_pct IS 'Muscular load split %. NULL from API (app-only / screenshot).';
COMMENT ON COLUMN public.whoop_workouts.whoop_id IS 'WHOOP API workout UUID — stable upsert key (profile_id, whoop_id).';
COMMENT ON COLUMN public.whoop_workouts.protocol IS 'JSONB intended-session spec, e.g. {"type":"4x4","pct_max":[90,95],"rounds":3,"work_min":4,"recovery_min":3}. Distinct from tags.';

-- ============ workout_intervals ============
COMMENT ON TABLE  public.workout_intervals IS 'Per-interval HR shape (peak/recovery) for VO2 tracking. TRAP: source=''screenshot'' = APPROXIMATE vision read; the API does NOT expose this. Additive only.';
COMMENT ON COLUMN public.workout_intervals.id IS 'PK.';
COMMENT ON COLUMN public.workout_intervals.profile_id IS 'FK profiles.id.';
COMMENT ON COLUMN public.workout_intervals.workout_id IS 'FK whoop_workouts.id this interval belongs to.';
COMMENT ON COLUMN public.workout_intervals.interval_index IS 'Ordinal of the interval within the workout (0-based).';
COMMENT ON COLUMN public.workout_intervals.kind IS 'work | recovery — interval type.';
COMMENT ON COLUMN public.workout_intervals.start_offset_sec IS 'Seconds from workout start.';
COMMENT ON COLUMN public.workout_intervals.duration_sec IS 'Interval duration (seconds).';
COMMENT ON COLUMN public.workout_intervals.peak_hr_bpm IS 'Peak HR in the interval (bpm). APPROXIMATE if source=screenshot.';
COMMENT ON COLUMN public.workout_intervals.avg_hr_bpm IS 'Average HR in the interval (bpm).';
COMMENT ON COLUMN public.workout_intervals.min_hr_bpm IS 'Min HR in the interval (bpm).';
COMMENT ON COLUMN public.workout_intervals.time_to_peak_sec IS 'Seconds from interval start to peak HR.';
COMMENT ON COLUMN public.workout_intervals.recovery_hr_bpm IS 'HR at the recovery valley after this interval (bpm).';
COMMENT ON COLUMN public.workout_intervals.recovery_drop_bpm IS 'peak_hr − recovery valley (bpm); higher = better recovery.';
COMMENT ON COLUMN public.workout_intervals.notes IS 'Free text.';
COMMENT ON COLUMN public.workout_intervals.source IS 'TRAP: ''screenshot'' = approximate (±5-10 bpm vision read) or ''manual''. Never measured telemetry.';
COMMENT ON COLUMN public.workout_intervals.created_at IS 'Row insert time.';

-- ============ workout_hr_samples ============
COMMENT ON TABLE  public.workout_hr_samples IS 'Digitised HR trace (~5-10s points) for a workout. TRAP: source=screenshot = APPROXIMATE curve-read, not per-second telemetry (API does not expose the trace).';
COMMENT ON COLUMN public.workout_hr_samples.id IS 'PK (bigint).';
COMMENT ON COLUMN public.workout_hr_samples.profile_id IS 'FK profiles.id.';
COMMENT ON COLUMN public.workout_hr_samples.workout_id IS 'FK whoop_workouts.id.';
COMMENT ON COLUMN public.workout_hr_samples.t_offset_sec IS 'Seconds from workout start.';
COMMENT ON COLUMN public.workout_hr_samples.hr_bpm IS 'HR at that offset (bpm). APPROXIMATE (screenshot curve digitisation).';

-- ============ hr_zone_config ============
COMMENT ON TABLE  public.hr_zone_config IS 'Per-profile HR zone boundaries, versioned by effective_date. Used to interpret zone seconds vs %-max.';
COMMENT ON COLUMN public.hr_zone_config.id IS 'PK.';
COMMENT ON COLUMN public.hr_zone_config.profile_id IS 'FK profiles.id.';
COMMENT ON COLUMN public.hr_zone_config.effective_date IS 'Date this calibration took effect (use the latest <= measurement date).';
COMMENT ON COLUMN public.hr_zone_config.max_hr_bpm IS 'Max HR (bpm) used for %-max zone math (PC=181 as of 2026-05-03).';
COMMENT ON COLUMN public.hr_zone_config.z0_max_bpm IS 'Zone 0 upper bound (bpm).';
COMMENT ON COLUMN public.hr_zone_config.z1_low_bpm IS 'Zone 1 lower bound (bpm).';
COMMENT ON COLUMN public.hr_zone_config.z1_high_bpm IS 'Zone 1 upper bound (bpm).';
COMMENT ON COLUMN public.hr_zone_config.z2_low_bpm IS 'Zone 2 lower bound (bpm).';
COMMENT ON COLUMN public.hr_zone_config.z2_high_bpm IS 'Zone 2 upper bound (bpm).';
COMMENT ON COLUMN public.hr_zone_config.z3_low_bpm IS 'Zone 3 lower bound (bpm).';
COMMENT ON COLUMN public.hr_zone_config.z3_high_bpm IS 'Zone 3 upper bound (bpm).';
COMMENT ON COLUMN public.hr_zone_config.z4_low_bpm IS 'Zone 4 lower bound (bpm) — threshold floor.';
COMMENT ON COLUMN public.hr_zone_config.z4_high_bpm IS 'Zone 4 upper bound (bpm).';
COMMENT ON COLUMN public.hr_zone_config.z5_low_bpm IS 'Zone 5 lower bound (bpm) — >95% max.';
COMMENT ON COLUMN public.hr_zone_config.source IS 'Origin of the calibration (whoop, manual).';
COMMENT ON COLUMN public.hr_zone_config.notes IS 'Free text.';
COMMENT ON COLUMN public.hr_zone_config.created_at IS 'Row insert time.';

-- ============ biomarkers ============
COMMENT ON TABLE  public.biomarkers IS 'All lab + DEXA scalar values (canonical home; body_metrics_history is dormant). Upsert key (profile_id, metric_definition_id, measured_at).';
COMMENT ON COLUMN public.biomarkers.id IS 'PK.';
COMMENT ON COLUMN public.biomarkers.user_id IS 'LEGACY auth.users id — DORMANT. Use profile_id.';
COMMENT ON COLUMN public.biomarkers.metric_definition_id IS 'FK metric_definitions.id — which marker. Canonical names: albumin_globulin_ratio / android_gynoid_ratio / ggt (NOT ag_ratio/ggtp).';
COMMENT ON COLUMN public.biomarkers.value IS 'Numeric result. TRAP: if qualifier is set (<,>) this is a detection BOUND, not an exact measurement.';
COMMENT ON COLUMN public.biomarkers.unit IS 'Unit as stored. TRAP: vitamin_d ng/mL (not nmol/L); hsCRP mg/L; testosterone ng/dL; lean/fat mass kg.';
COMMENT ON COLUMN public.biomarkers.source IS 'Origin: manual_import | manual_skill | lab report etc.';
COMMENT ON COLUMN public.biomarkers.document_id IS 'FK documents.id (source PDF) if any. Usually NULL.';
COMMENT ON COLUMN public.biomarkers.staging_id IS 'FK stg_biomarker_review.id if promoted from staging.';
COMMENT ON COLUMN public.biomarkers.measured_at IS 'When the sample was taken (timestamptz, from the report — NEVER now()). Part of the upsert key.';
COMMENT ON COLUMN public.biomarkers.lab_name IS 'Lab/source (Max Lab, Dr Lal PathLabs, Metropolis, Miracles Healthcare, WHOOP...).';
COMMENT ON COLUMN public.biomarkers.notes IS 'Context incl. conversions/derivations. TRAP: HOMA-IR/eGFR/ratios are lab-PRINTED derived values — NEVER recompute (Rule #7); note records the model (e.g. HOMA2).';
COMMENT ON COLUMN public.biomarkers.created_at IS 'Row insert time.';
COMMENT ON COLUMN public.biomarkers.profile_id IS 'FK profiles.id — live per-person key.';
COMMENT ON COLUMN public.biomarkers.healthspan_test_id IS 'FK healthspan_tests.id (investigation grouping) if any.';
COMMENT ON COLUMN public.biomarkers.qualifier IS 'Below/above-detection operator: ''<'' value is an upper bound (e.g. <10), ''>'' a lower bound. NULL = exact. PRESERVE in display.';

-- ============ metric_definitions ============
COMMENT ON TABLE  public.metric_definitions IS 'Catalog of measurable markers (149 rows). SELECT-only for the skill role; INSERT is owner-only (non-owner flags unknowns).';
COMMENT ON COLUMN public.metric_definitions.id IS 'PK.';
COMMENT ON COLUMN public.metric_definitions.name IS 'Canonical snake_case name (the join key). Use albumin_globulin_ratio/android_gynoid_ratio/ggt.';
COMMENT ON COLUMN public.metric_definitions.display_name IS 'Human label.';
COMMENT ON COLUMN public.metric_definitions.category IS 'Grouping: blood_count, lipids, hormones, metabolic, liver, kidney, minerals, cardiac, body_composition, bone_density, etc.';
COMMENT ON COLUMN public.metric_definitions.data_type IS 'numeric | text — value type.';
COMMENT ON COLUMN public.metric_definitions.unit IS 'Canonical unit for this marker.';
COMMENT ON COLUMN public.metric_definitions.loinc_id IS 'FK loinc_reference.id if mapped.';
COMMENT ON COLUMN public.metric_definitions.description IS 'What the marker means clinically.';
COMMENT ON COLUMN public.metric_definitions.min_value IS 'Reference range low (for out-of-range flagging). NULL = no low bound.';
COMMENT ON COLUMN public.metric_definitions.max_value IS 'Reference range high. NULL = no high bound. TRAP: ratio/index markers (Mentzer) have a soft cap, not a true clinical range.';
COMMENT ON COLUMN public.metric_definitions.decimal_places IS 'Display precision.';
COMMENT ON COLUMN public.metric_definitions.is_active IS 'Whether this definition is in use.';
COMMENT ON COLUMN public.metric_definitions.created_at IS 'Row insert time.';
COMMENT ON COLUMN public.metric_definitions.updated_at IS 'Last update time.';

-- ============ food_logs ============
COMMENT ON TABLE  public.food_logs IS 'Meal entries (macros, verdict, Viome flags). is_day_summary rows hold a day total, not a meal.';
COMMENT ON COLUMN public.food_logs.id IS 'PK.';
COMMENT ON COLUMN public.food_logs.user_id IS 'LEGACY auth.users id — DORMANT. Use profile_id.';
COMMENT ON COLUMN public.food_logs.meal_type IS 'breakfast | lunch | dinner | snack | drink | supplement (CHECK).';
COMMENT ON COLUMN public.food_logs.description IS 'Meal description (free text).';
COMMENT ON COLUMN public.food_logs.calories IS 'kcal. TRAP: must be parsed with thousands separators stripped ("1,020"→1020), never comma-truncated.';
COMMENT ON COLUMN public.food_logs.protein_g IS 'Protein (g).';
COMMENT ON COLUMN public.food_logs.carbs_g IS 'Carbohydrate (g).';
COMMENT ON COLUMN public.food_logs.fat_g IS 'Fat (g).';
COMMENT ON COLUMN public.food_logs.fiber_g IS 'Fibre (g).';
COMMENT ON COLUMN public.food_logs.source IS 'Origin: manual_skill | manual_import | photo etc.';
COMMENT ON COLUMN public.food_logs.logged_at IS 'Legacy timestamp. Prefer log_date + meal_time.';
COMMENT ON COLUMN public.food_logs.image_url IS 'Source photo URL if any.';
COMMENT ON COLUMN public.food_logs.notes IS 'Free text.';
COMMENT ON COLUMN public.food_logs.created_at IS 'Row insert time.';
COMMENT ON COLUMN public.food_logs.log_date IS 'Date of the meal (date).';
COMMENT ON COLUMN public.food_logs.meal_time IS 'Time of the meal (time).';
COMMENT ON COLUMN public.food_logs.location IS 'Where eaten.';
COMMENT ON COLUMN public.food_logs.foods IS 'JSONB array of resolved food items.';
COMMENT ON COLUMN public.food_logs.verdict IS 'GREEN/AMBER/RED or similar diet-fit verdict.';
COMMENT ON COLUMN public.food_logs.flags IS 'TEXT[] of dietary flags (e.g. Viome avoid/minimize hits).';
COMMENT ON COLUMN public.food_logs.is_day_summary IS 'TRAP: TRUE = a whole-day total row, not a single meal. Exclude from per-meal analysis; use for daily macros.';
COMMENT ON COLUMN public.food_logs.sprint_id IS 'FK sprints.id (auto-tagged).';
COMMENT ON COLUMN public.food_logs.source_photo_path IS 'Path of the source food photo.';
COMMENT ON COLUMN public.food_logs.source_log_path IS 'Path of the source log file. Part of the file-sourced upsert key (with log_date, description).';
COMMENT ON COLUMN public.food_logs.profile_id IS 'FK profiles.id — live per-person key.';

-- ============ supplement_regimens ============
COMMENT ON TABLE  public.supplement_regimens IS 'A person''s supplement protocol (dose/timing/duration, cyclical aware). status: active|discontinued|planned. No general status enum beyond CHECK.';
COMMENT ON COLUMN public.supplement_regimens.id IS 'PK.';
COMMENT ON COLUMN public.supplement_regimens.profile_id IS 'FK profiles.id.';
COMMENT ON COLUMN public.supplement_regimens.supplement_id IS 'FK supplements.id.';
COMMENT ON COLUMN public.supplement_regimens.dose_amount IS 'Dose quantity (numeric).';
COMMENT ON COLUMN public.supplement_regimens.dose_unit IS 'Dose unit (mg, mcg, g, IU, serving...).';
COMMENT ON COLUMN public.supplement_regimens.timing IS 'TEXT[] of timings (wake, breakfast, lunch, dinner, bedtime, anytime).';
COMMENT ON COLUMN public.supplement_regimens.frequency IS 'daily | cyclical etc.';
COMMENT ON COLUMN public.supplement_regimens.cycle_days_on IS 'Cyclical: days on.';
COMMENT ON COLUMN public.supplement_regimens.cycle_days_off IS 'Cyclical: days off.';
COMMENT ON COLUMN public.supplement_regimens.start_date IS 'Protocol start (from source, never now()).';
COMMENT ON COLUMN public.supplement_regimens.end_date IS 'Protocol end (NULL = ongoing).';
COMMENT ON COLUMN public.supplement_regimens.status IS 'active | discontinued | planned.';
COMMENT ON COLUMN public.supplement_regimens.purpose IS 'Why it''s taken.';
COMMENT ON COLUMN public.supplement_regimens.sprint_id IS 'FK sprints.id if tied to a training block.';
COMMENT ON COLUMN public.supplement_regimens.program_id IS 'FK training_programs.id if tied to a program.';
COMMENT ON COLUMN public.supplement_regimens.notes IS 'Free text.';
COMMENT ON COLUMN public.supplement_regimens.created_at IS 'Row insert time.';
COMMENT ON COLUMN public.supplement_regimens.updated_at IS 'Last update.';

-- ============ supplement_intake_logs ============
COMMENT ON TABLE  public.supplement_intake_logs IS 'Actual supplement intake events. Upsert key (profile_id, supplement_id, taken_on, source). source CHECK: manual|journal|skill|csv.';
COMMENT ON COLUMN public.supplement_intake_logs.id IS 'PK.';
COMMENT ON COLUMN public.supplement_intake_logs.profile_id IS 'FK profiles.id.';
COMMENT ON COLUMN public.supplement_intake_logs.regimen_id IS 'FK supplement_regimens.id (auto-attached from active regimen).';
COMMENT ON COLUMN public.supplement_intake_logs.supplement_id IS 'FK supplements.id.';
COMMENT ON COLUMN public.supplement_intake_logs.taken_at IS 'Intake timestamp (timestamptz).';
COMMENT ON COLUMN public.supplement_intake_logs.taken_on IS 'Intake date (date) — part of the upsert key.';
COMMENT ON COLUMN public.supplement_intake_logs.dose_amount IS 'Dose taken.';
COMMENT ON COLUMN public.supplement_intake_logs.dose_unit IS 'Dose unit.';
COMMENT ON COLUMN public.supplement_intake_logs.source IS 'CHECK: manual | journal | skill | csv. Default skill.';
COMMENT ON COLUMN public.supplement_intake_logs.notes IS 'Free text.';
COMMENT ON COLUMN public.supplement_intake_logs.created_at IS 'Row insert time.';

-- ============ health_context_notes ============
COMMENT ON TABLE  public.health_context_notes IS 'Skill session memory. TRAP: notes are NARRATIVE + references to numbers — NEVER store the numbers themselves (always pull live from the DB). One ''current'' note per profile + short ''log'' notes.';
COMMENT ON COLUMN public.health_context_notes.id IS 'PK.';
COMMENT ON COLUMN public.health_context_notes.profile_id IS 'FK profiles.id.';
COMMENT ON COLUMN public.health_context_notes.kind IS 'current (single superseding summary per profile) | log (recent, keep last ~5).';
COMMENT ON COLUMN public.health_context_notes.summary IS 'Narrative/state ONLY. No numbers — reference the DB instead.';
COMMENT ON COLUMN public.health_context_notes.refs IS 'JSONB pointers to where numbers live (e.g. {"biomarkers":"2026-06-02 DEXA"}). Not the numbers.';
COMMENT ON COLUMN public.health_context_notes.s3_keys IS 'TEXT[] optional external MD files on S3.';
COMMENT ON COLUMN public.health_context_notes.supersedes_id IS 'Self-FK: the note this one replaces.';
COMMENT ON COLUMN public.health_context_notes.created_at IS 'Row insert time.';

-- ============ sprints ============
COMMENT ON TABLE  public.sprints IS 'Training blocks (location+date window). TRAP: NO status column — derive active/completed/upcoming from start_date/end_date vs today.';
COMMENT ON COLUMN public.sprints.id IS 'PK.';
COMMENT ON COLUMN public.sprints.user_id IS 'LEGACY auth.users id — DORMANT. Use profile_id.';
COMMENT ON COLUMN public.sprints.name IS 'Sprint name.';
COMMENT ON COLUMN public.sprints.slug IS 'Unique slug per profile (upsert key with profile_id).';
COMMENT ON COLUMN public.sprints.location IS 'Where the block happens.';
COMMENT ON COLUMN public.sprints.start_date IS 'Block start (date). Used for auto-tagging cycles/workouts and deriving status.';
COMMENT ON COLUMN public.sprints.end_date IS 'Block end (date).';
COMMENT ON COLUMN public.sprints.goals IS 'TEXT[] of goals for the block.';
COMMENT ON COLUMN public.sprints.notes IS 'Free text.';
COMMENT ON COLUMN public.sprints.created_at IS 'Row insert time.';
COMMENT ON COLUMN public.sprints.profile_id IS 'FK profiles.id — live per-person key.';

-- ============ travel_log ============
COMMENT ON TABLE  public.travel_log IS 'Trips for location-based performance analysis (feeds v_india_vs_travel). Upsert key (profile_id, start_date, destination).';
COMMENT ON COLUMN public.travel_log.id IS 'PK.';
COMMENT ON COLUMN public.travel_log.profile_id IS 'FK profiles.id.';
COMMENT ON COLUMN public.travel_log.start_date IS 'Trip start (date).';
COMMENT ON COLUMN public.travel_log.end_date IS 'Trip end (date).';
COMMENT ON COLUMN public.travel_log.destination IS 'Destination name.';
COMMENT ON COLUMN public.travel_log.country_code IS 'ISO country code.';
COMMENT ON COLUMN public.travel_log.alcohol_level IS 'Subjective alcohol intake during the trip.';
COMMENT ON COLUMN public.travel_log.activity_context IS 'Trip activity context (e.g. trek, business).';
COMMENT ON COLUMN public.travel_log.is_altitude IS 'TRUE = high-altitude trip (affects recovery/SpO2).';
COMMENT ON COLUMN public.travel_log.timezone_hint IS 'Timezone hint for the location.';
COMMENT ON COLUMN public.travel_log.notes IS 'Free text.';
COMMENT ON COLUMN public.travel_log.created_at IS 'Row insert time.';

-- ============ profiles ============
COMMENT ON TABLE  public.profiles IS 'One row per tracked person. auth_user_id nullable (children with no login). Health data keys to profile_id; access via family_memberships + has_profile_access().';
COMMENT ON COLUMN public.profiles.id IS 'PK — the profile_id used across all health tables.';
COMMENT ON COLUMN public.profiles.auth_user_id IS 'FK auth.users — set if/when this person has a login (NULL for no-login children). RLS resolves via this.';
COMMENT ON COLUMN public.profiles.family_id IS 'FK families.id.';
COMMENT ON COLUMN public.profiles.managed_by_auth_user_id IS 'Owner who manages this profile (e.g. PC manages Dea/Dev).';
COMMENT ON COLUMN public.profiles.display_name IS 'Display name (PC, Dea Singh Chitalkar, ...).';
COMMENT ON COLUMN public.profiles.date_of_birth IS 'DOB (date) — for age-specific reference ranges.';
COMMENT ON COLUMN public.profiles.sex IS 'Biological sex — for sex-specific reference ranges.';
COMMENT ON COLUMN public.profiles.relationship IS 'self | child | member.';
COMMENT ON COLUMN public.profiles.is_active IS 'Whether the profile is active.';
COMMENT ON COLUMN public.profiles.created_at IS 'Row insert time.';
COMMENT ON COLUMN public.profiles.updated_at IS 'Last update.';

-- ============ families ============
COMMENT ON TABLE  public.families IS 'Household grouping for multi-tenant access.';
COMMENT ON COLUMN public.families.id IS 'PK.';
COMMENT ON COLUMN public.families.name IS 'Family name.';
COMMENT ON COLUMN public.families.created_by IS 'FK auth.users who created it.';
COMMENT ON COLUMN public.families.created_at IS 'Row insert time.';
COMMENT ON COLUMN public.families.updated_at IS 'Last update.';

-- ============ family_memberships ============
COMMENT ON TABLE  public.family_memberships IS 'Grants an auth user access to a profile. has_profile_access() reads this. UNIQUE(auth_user_id, profile_id).';
COMMENT ON COLUMN public.family_memberships.id IS 'PK.';
COMMENT ON COLUMN public.family_memberships.auth_user_id IS 'FK auth.users — who has access.';
COMMENT ON COLUMN public.family_memberships.profile_id IS 'FK profiles.id — the profile they can access.';
COMMENT ON COLUMN public.family_memberships.role IS 'owner | member | self — owner can manage the profile.';
COMMENT ON COLUMN public.family_memberships.created_at IS 'Row insert time.';

-- ============ wearable_sync_log ============
COMMENT ON TABLE  public.wearable_sync_log IS 'One row per ingestion run (ground truth for "did it run, what happened"). status CHECK: pending|in_progress|success|failed.';
COMMENT ON COLUMN public.wearable_sync_log.id IS 'PK.';
COMMENT ON COLUMN public.wearable_sync_log.user_id IS 'LEGACY auth.users id — DORMANT. Use profile_id.';
COMMENT ON COLUMN public.wearable_sync_log.provider IS 'whoop, ultrahuman, etc.';
COMMENT ON COLUMN public.wearable_sync_log.sync_type IS 'What synced (whoop_workouts, webhook:workout.updated, etc.).';
COMMENT ON COLUMN public.wearable_sync_log.status IS 'pending | in_progress | success | failed (CHECK). NOT ''running''.';
COMMENT ON COLUMN public.wearable_sync_log.records_synced IS 'Legacy counter. Prefer records_in/upserted/skipped/failed.';
COMMENT ON COLUMN public.wearable_sync_log.error_message IS 'Run-level error if any.';
COMMENT ON COLUMN public.wearable_sync_log.started_at IS 'Run start.';
COMMENT ON COLUMN public.wearable_sync_log.completed_at IS 'Run end.';
COMMENT ON COLUMN public.wearable_sync_log.created_at IS 'Row insert time.';
COMMENT ON COLUMN public.wearable_sync_log.profile_id IS 'FK profiles.id.';
COMMENT ON COLUMN public.wearable_sync_log.method IS 'csv | screenshot | photo | manual | api (CHECK).';
COMMENT ON COLUMN public.wearable_sync_log.records_in IS 'Records read from the source.';
COMMENT ON COLUMN public.wearable_sync_log.records_upserted IS 'Records written (insert+update).';
COMMENT ON COLUMN public.wearable_sync_log.records_skipped IS 'Records skipped (already present / no match).';
COMMENT ON COLUMN public.wearable_sync_log.records_failed IS 'Records that errored (see wearable_sync_errors).';
COMMENT ON COLUMN public.wearable_sync_log.document_id IS 'FK documents.id if a PDF source.';
COMMENT ON COLUMN public.wearable_sync_log.source_path IS 'Source file/path or descriptor.';

-- ============ wearable_sync_errors ============
COMMENT ON TABLE  public.wearable_sync_errors IS 'Per-record ingestion failures (one row per bad record). Click a failed run to see exactly which rows died and why.';
COMMENT ON COLUMN public.wearable_sync_errors.id IS 'PK.';
COMMENT ON COLUMN public.wearable_sync_errors.sync_log_id IS 'FK wearable_sync_log.id.';
COMMENT ON COLUMN public.wearable_sync_errors.record_ref IS 'Identifier of the failing record.';
COMMENT ON COLUMN public.wearable_sync_errors.error_code IS 'Short error category.';
COMMENT ON COLUMN public.wearable_sync_errors.error_message IS 'Error detail.';
COMMENT ON COLUMN public.wearable_sync_errors.raw IS 'JSONB of the raw record that failed.';
COMMENT ON COLUMN public.wearable_sync_errors.created_at IS 'Row insert time.';

COMMIT;
