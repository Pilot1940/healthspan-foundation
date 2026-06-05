# HealthSpan — Schema Map (semantic reference)

> **GENERATED from pg_description (column/table COMMENTs, migration 016). Do NOT hand-edit — re-run `scripts/gen_schema_map.py`.** The skill loads this before composing any ad-hoc SQL.
> Generated 2026-06-05 05:18 UTC.
> generated_at: 2026-06-05T05:18:36Z

## `profiles`
_One row per tracked person. auth_user_id nullable (children with no login). Health data keys to profile_id; access via family_memberships + has_profile_access()._

| column | type | meaning / unit / trap |
|---|---|---|
| `id` | uuid | PK — the profile_id used across all health tables. |
| `auth_user_id` | uuid | FK auth.users — set if/when this person has a login (NULL for no-login children). RLS resolves via this. |
| `family_id` | uuid | FK families.id. |
| `managed_by_auth_user_id` | uuid | Owner who manages this profile (e.g. PC manages Dea/Dev). |
| `display_name` | text | Display name (PC, Dea Singh Chitalkar, ...). |
| `date_of_birth` | date | DOB (date) — for age-specific reference ranges. |
| `sex` | text | Biological sex — for sex-specific reference ranges. |
| `relationship` | text | self \| child \| member. |
| `is_active` | boolean | Whether the profile is active. |
| `created_at` | timestamp with time zone | Row insert time. |
| `updated_at` | timestamp with time zone | Last update. |
| `is_maintainer` | boolean |  |

## `families`
_Household grouping for multi-tenant access._

| column | type | meaning / unit / trap |
|---|---|---|
| `id` | uuid | PK. |
| `name` | text | Family name. |
| `created_by` | uuid | FK auth.users who created it. |
| `created_at` | timestamp with time zone | Row insert time. |
| `updated_at` | timestamp with time zone | Last update. |

## `family_memberships`
_Grants an auth user access to a profile. has_profile_access() reads this. UNIQUE(auth_user_id, profile_id)._

| column | type | meaning / unit / trap |
|---|---|---|
| `id` | uuid | PK. |
| `auth_user_id` | uuid | FK auth.users — who has access. |
| `profile_id` | uuid | FK profiles.id — the profile they can access. |
| `role` | text | owner \| member \| self — owner can manage the profile. |
| `created_at` | timestamp with time zone | Row insert time. |

## `whoop_cycles`
_WHOOP physiological day (wake-to-wake): one row per cycle. Recovery/HRV/RHR derive from that night's sleep. Keyed on (profile_id, whoop_id)._

| column | type | meaning / unit / trap |
|---|---|---|
| `id` | uuid | PK (gen_random_uuid). |
| `user_id` | uuid | LEGACY auth.users id — DORMANT. Use profile_id as the live key. |
| `cycle_start` | timestamp with time zone | Start of the WHOOP physiological day (timestamptz, UTC). TRAP: this is the WHOOP cycle boundary = screenshot date MINUS 1 day; not the calendar date. JOIN TRAP: to join whoop_journal (CSV-sourced timestamps) join on cycle_start::date, NOT the exact timestamp — exact match returns 0 rows. |
| `cycle_end` | timestamp with time zone | End of the WHOOP cycle (timestamptz). |
| `timezone` | text | WHOOP timezone_offset string (e.g. +05:30) at cycle time. |
| `recovery_score_pct` | numeric(5,2) | Recovery score 0-100. TRAP: NULL = NO SLEEP that cycle (strap off / nap-only) — EXCLUDE from averages/trends, NEVER treat as 0. |
| `resting_hr_bpm` | numeric(5,1) | Resting heart rate (bpm). TRAP: NULL = no sleep that cycle; exclude, never average as 0. |
| `hrv_ms` | numeric(5,1) | HRV RMSSD (ms). TRAP: NULL = no sleep that cycle; exclude, never average as 0. |
| `skin_temp_celsius` | numeric(5,2) | Skin temperature (°C) from recovery. |
| `blood_oxygen_pct` | numeric(5,2) | SpO2 (%) from recovery. |
| `respiratory_rate_rpm` | numeric(5,2) | Respiratory rate (breaths/min) from sleep. |
| `day_strain` | numeric(5,2) | WHOOP day strain 0-21 (logarithmic). TRAP: strain present + recovery NULL = strap worn in day but not overnight (wear-compliance gap, not health regression). |
| `energy_burned_cal` | numeric(8,1) | Energy burned for the cycle (kcal; converted from WHOOP kilojoule). |
| `max_hr_bpm` | integer | Max HR during the cycle (bpm). |
| `avg_hr_bpm` | integer | Average HR during the cycle (bpm). |
| `sleep_onset` | timestamp with time zone | Main sleep onset for this cycle (timestamptz). |
| `wake_onset` | timestamp with time zone | Wake time for this cycle (timestamptz). |
| `sleep_performance_pct` | numeric(5,2) | Sleep performance % (asleep vs needed). |
| `asleep_duration_min` | numeric(6,1) | Total asleep time (minutes). |
| `in_bed_duration_min` | numeric(6,1) | Time in bed (minutes). |
| `light_sleep_min` | numeric(6,1) | Light sleep (minutes). |
| `deep_sws_min` | numeric(6,1) | Deep / slow-wave sleep (minutes). |
| `rem_min` | numeric(6,1) | REM sleep (minutes). |
| `awake_min` | numeric(6,1) | Awake time during sleep (minutes). |
| `sleep_need_min` | numeric(6,1) | WHOOP-calculated sleep need (minutes). |
| `sleep_debt_min` | numeric(6,1) | Sleep debt (minutes). |
| `sleep_efficiency_pct` | numeric(5,2) | Sleep efficiency %. |
| `sleep_consistency_pct` | numeric(5,2) | Sleep consistency %. |
| `sprint_id` | uuid | FK sprints.id — auto-tagged by date-overlap trigger (training block this cycle falls in). |
| `source_file` | text | Origin: whoop_api \| whoop_webhook \| screenshot \| CSV export name. |
| `created_at` | timestamp with time zone | Row insert time (not the cycle date). |
| `profile_id` | uuid | FK profiles.id — the LIVE per-person key (RLS scopes on this). |
| `whoop_id` | text | WHOOP API cycle UUID — the stable upsert key (profile_id, whoop_id). Re-scores overwrite, never duplicate. |

## `whoop_sleeps`
_Individual sleep events incl. naps. Keyed on (profile_id, whoop_id). Most analyses filter is_nap=false._

| column | type | meaning / unit / trap |
|---|---|---|
| `id` | uuid | PK. |
| `user_id` | uuid | LEGACY auth.users id — DORMANT. Use profile_id. |
| `cycle_start` | timestamp with time zone | Owning cycle start (timestamptz); links a sleep to its WHOOP cycle. |
| `cycle_end` | timestamp with time zone | Owning cycle end (timestamptz). |
| `timezone` | text | WHOOP timezone_offset at sleep time. |
| `sleep_onset` | timestamp with time zone | Sleep start (timestamptz). |
| `wake_onset` | timestamp with time zone | Wake time (timestamptz). |
| `sleep_performance_pct` | numeric(5,2) | Sleep performance %. |
| `respiratory_rate_rpm` | numeric(5,2) | Respiratory rate (breaths/min). |
| `asleep_duration_min` | numeric(6,1) | Asleep time (minutes). |
| `in_bed_duration_min` | numeric(6,1) | Time in bed (minutes). |
| `light_sleep_min` | numeric(6,1) | Light sleep (minutes). |
| `deep_sws_min` | numeric(6,1) | Deep / slow-wave sleep (minutes). |
| `rem_min` | numeric(6,1) | REM sleep (minutes). |
| `awake_min` | numeric(6,1) | Awake time (minutes). |
| `sleep_need_min` | numeric(6,1) | Sleep need (minutes). |
| `sleep_debt_min` | numeric(6,1) | Sleep debt (minutes). |
| `sleep_efficiency_pct` | numeric(5,2) | Sleep efficiency %. |
| `sleep_consistency_pct` | numeric(5,2) | Sleep consistency %. |
| `is_nap` | boolean | TRAP: TRUE = nap, separate from the main overnight sleep. Most sleep analyses filter is_nap=false. |
| `sprint_id` | uuid | FK sprints.id (auto-tagged). |
| `source_file` | text | Origin (whoop_api/webhook/screenshot/CSV). |
| `created_at` | timestamp with time zone | Row insert time. |
| `profile_id` | uuid | FK profiles.id — live per-person key. |
| `whoop_id` | text | WHOOP API sleep UUID — stable upsert key (profile_id, whoop_id). |

## `whoop_workouts`
_Workouts with zone durations. Keyed on (profile_id, whoop_id). Zone seconds (hr_zoneN_sec) are exact from API; percentages are derived._

| column | type | meaning / unit / trap |
|---|---|---|
| `id` | uuid | PK. |
| `user_id` | uuid | LEGACY auth.users id — DORMANT. Use profile_id. |
| `cycle_start` | timestamp with time zone | Owning WHOOP cycle start (timestamptz). |
| `cycle_end` | timestamp with time zone | Owning cycle end. |
| `timezone` | text | WHOOP timezone_offset at workout time. |
| `workout_start` | timestamp with time zone | Workout start (timestamptz, UTC). Render in timezone for local wall-clock. |
| `workout_end` | timestamp with time zone | Workout end (timestamptz). |
| `duration_min` | numeric(6,1) | Duration (minutes). |
| `activity_name` | text | Sport/activity name (WHOOP sport_name, e.g. functional-fitness, running, walking). |
| `activity_strain` | numeric(5,2) | Workout strain 0-21 (logarithmic). |
| `energy_burned_cal` | numeric(8,1) | Energy burned (kcal; from kilojoule). |
| `max_hr_bpm` | integer | Max HR (bpm). |
| `avg_hr_bpm` | integer | Average HR (bpm). |
| `hr_zone1_pct` | numeric(5,2) | Zone 1 % of workout (derived). Zone *_sec are the precise source. |
| `hr_zone2_pct` | numeric(5,2) | Zone 2 % (derived). |
| `hr_zone3_pct` | numeric(5,2) | Zone 3 % (derived). |
| `hr_zone4_pct` | numeric(5,2) | Zone 4 % (derived). Z4=threshold (90-95% max in 4x4 protocols). |
| `hr_zone5_pct` | numeric(5,2) | Zone 5 % (derived). Z5=>95% max; high Z5 share vs Z4 = redlining. |
| `gps_enabled` | boolean | Whether GPS was on. |
| `sprint_id` | uuid | FK sprints.id (auto-tagged). |
| `source_file` | text | Origin (whoop_api/webhook/screenshot/CSV). |
| `created_at` | timestamp with time zone | Row insert time. |
| `profile_id` | uuid | FK profiles.id — live per-person key. |
| `tags` | jsonb | JSONB array of program/label tags. Coach-note from screenshots stored here as {coach_note,...}. |
| `hr_zone0_pct` | numeric(5,2) | Zone 0 (<110 bpm) % (derived). |
| `hr_zone0_sec` | integer | Zone 0 seconds (<110 bpm). From API zone_durations. |
| `hr_zone1_sec` | integer | Zone 1 seconds (recovery). Precise from API. |
| `hr_zone2_sec` | integer | Zone 2 seconds (aerobic base). Precise from API. |
| `hr_zone3_sec` | integer | Zone 3 seconds (tempo / transition). Precise from API. |
| `hr_zone4_sec` | integer | Zone 4 seconds (threshold, 90-95% max). Precise from API. |
| `hr_zone5_sec` | integer | Zone 5 seconds (>95% max). Precise from API. |
| `cardio_load_pct` | numeric(5,2) | Cardio load split %. NULL from API (app-only / screenshot). |
| `muscular_load_pct` | numeric(5,2) | Muscular load split %. NULL from API (app-only / screenshot). |
| `whoop_id` | text | WHOOP API workout UUID — stable upsert key (profile_id, whoop_id). |
| `protocol` | jsonb | JSONB intended-session spec, e.g. {"type":"4x4","pct_max":[90,95],"rounds":3,"work_min":4,"recovery_min":3}. Distinct from tags. |

## `workout_intervals`
_Per-interval HR shape (peak/recovery) for VO2 tracking. TRAP: source='screenshot' = APPROXIMATE vision read; the API does NOT expose this. Additive only._

| column | type | meaning / unit / trap |
|---|---|---|
| `id` | uuid | PK. |
| `profile_id` | uuid | FK profiles.id. |
| `workout_id` | uuid | FK whoop_workouts.id this interval belongs to. |
| `interval_index` | integer | Ordinal of the interval within the workout (0-based). |
| `kind` | text | work \| recovery — interval type. |
| `start_offset_sec` | integer | Seconds from workout start. |
| `duration_sec` | integer | Interval duration (seconds). |
| `peak_hr_bpm` | integer | Peak HR in the interval (bpm). APPROXIMATE if source=screenshot. |
| `avg_hr_bpm` | integer | Average HR in the interval (bpm). |
| `min_hr_bpm` | integer | Min HR in the interval (bpm). |
| `time_to_peak_sec` | integer | Seconds from interval start to peak HR. |
| `recovery_hr_bpm` | integer | HR at the recovery valley after this interval (bpm). |
| `recovery_drop_bpm` | integer | peak_hr − recovery valley (bpm); higher = better recovery. |
| `notes` | text | Free text. |
| `source` | text | TRAP: 'screenshot' = approximate (±5-10 bpm vision read) or 'manual'. Never measured telemetry. |
| `created_at` | timestamp with time zone | Row insert time. |

## `workout_hr_samples`
_Digitised HR trace (~5-10s points) for a workout. TRAP: source=screenshot = APPROXIMATE curve-read, not per-second telemetry (API does not expose the trace)._

| column | type | meaning / unit / trap |
|---|---|---|
| `id` | bigint | PK (bigint). |
| `profile_id` | uuid | FK profiles.id. |
| `workout_id` | uuid | FK whoop_workouts.id. |
| `t_offset_sec` | integer | Seconds from workout start. |
| `hr_bpm` | smallint | HR at that offset (bpm). APPROXIMATE (screenshot curve digitisation). |

## `hr_zone_config`
_Per-profile HR zone boundaries, versioned by effective_date. Used to interpret zone seconds vs %-max._

| column | type | meaning / unit / trap |
|---|---|---|
| `id` | uuid | PK. |
| `profile_id` | uuid | FK profiles.id. |
| `effective_date` | date | Date this calibration took effect (use the latest <= measurement date). |
| `max_hr_bpm` | integer | Max HR (bpm) used for %-max zone math (PC=181 as of 2026-05-03). |
| `z0_max_bpm` | integer | Zone 0 upper bound (bpm). |
| `z1_low_bpm` | integer | Zone 1 lower bound (bpm). |
| `z1_high_bpm` | integer | Zone 1 upper bound (bpm). |
| `z2_low_bpm` | integer | Zone 2 lower bound (bpm). |
| `z2_high_bpm` | integer | Zone 2 upper bound (bpm). |
| `z3_low_bpm` | integer | Zone 3 lower bound (bpm). |
| `z3_high_bpm` | integer | Zone 3 upper bound (bpm). |
| `z4_low_bpm` | integer | Zone 4 lower bound (bpm) — threshold floor. |
| `z4_high_bpm` | integer | Zone 4 upper bound (bpm). |
| `z5_low_bpm` | integer | Zone 5 lower bound (bpm) — >95% max. |
| `source` | text | Origin of the calibration (whoop, manual). |
| `notes` | text | Free text. |
| `created_at` | timestamp with time zone | Row insert time. |

## `biomarkers`
_All lab + DEXA scalar values (canonical home; body_metrics_history is dormant). Upsert key (profile_id, metric_definition_id, measured_at)._

| column | type | meaning / unit / trap |
|---|---|---|
| `id` | uuid | PK. |
| `user_id` | uuid | LEGACY auth.users id — DORMANT. Use profile_id. |
| `metric_definition_id` | uuid | FK metric_definitions.id — which marker. Canonical names: albumin_globulin_ratio / android_gynoid_ratio / ggt (NOT ag_ratio/ggtp). |
| `value` | numeric | Numeric result. TRAP: if qualifier is set (<,>) this is a detection BOUND, not an exact measurement. |
| `unit` | text | Unit as stored. TRAP: vitamin_d ng/mL (not nmol/L); hsCRP mg/L; testosterone ng/dL; lean/fat mass kg. |
| `source` | text | Origin: manual_import \| manual_skill \| lab report etc. |
| `document_id` | uuid | FK documents.id (source PDF) if any. Usually NULL. |
| `staging_id` | uuid | FK stg_biomarker_review.id if promoted from staging. |
| `measured_at` | timestamp with time zone | When the sample was taken (timestamptz, from the report — NEVER now()). Part of the upsert key. |
| `lab_name` | text | Lab/source (Max Lab, Dr Lal PathLabs, Metropolis, Miracles Healthcare, WHOOP...). |
| `notes` | text | Context incl. conversions/derivations. TRAP: HOMA-IR/eGFR/ratios are lab-PRINTED derived values — NEVER recompute (Rule #7); note records the model (e.g. HOMA2). |
| `created_at` | timestamp with time zone | Row insert time. |
| `profile_id` | uuid | FK profiles.id — live per-person key. |
| `healthspan_test_id` | uuid | FK healthspan_tests.id (investigation grouping) if any. |
| `qualifier` | text | Below/above-detection operator: '<' value is an upper bound (e.g. <10), '>' a lower bound. NULL = exact. PRESERVE in display. |

## `metric_definitions`
_Catalog of measurable markers (149 rows). SELECT-only for the skill role; INSERT is owner-only (non-owner flags unknowns)._

| column | type | meaning / unit / trap |
|---|---|---|
| `id` | uuid | PK. |
| `name` | text | Canonical snake_case name (the join key). Use albumin_globulin_ratio/android_gynoid_ratio/ggt. |
| `display_name` | text | Human label. |
| `category` | text | Grouping: blood_count, lipids, hormones, metabolic, liver, kidney, minerals, cardiac, body_composition, bone_density, etc. |
| `data_type` | text | numeric \| text — value type. |
| `unit` | text | Canonical unit for this marker. |
| `loinc_id` | uuid | FK loinc_reference.id if mapped. |
| `description` | text | What the marker means clinically. |
| `min_value` | numeric | Reference range low (for out-of-range flagging). NULL = no low bound. |
| `max_value` | numeric | Reference range high. NULL = no high bound. TRAP: ratio/index markers (Mentzer) have a soft cap, not a true clinical range. |
| `decimal_places` | integer | Display precision. |
| `is_active` | boolean | Whether this definition is in use. |
| `created_at` | timestamp with time zone | Row insert time. |
| `updated_at` | timestamp with time zone | Last update time. |
| `plausible_min` | numeric | Physiologically POSSIBLE lower extreme (NOT the clinical reference min_value). A value BELOW this is gated → staging (likely OCR/unit/comma error). NULL = unknown → contract uses the per-(profile,metric) 10x/0.1x heuristic. Much wider than min_value. |
| `plausible_max` | numeric | Physiologically POSSIBLE upper extreme (NOT the clinical reference max_value). A value ABOVE this is gated → staging (likely OCR/unit/comma error). NULL = unknown → contract uses the per-(profile,metric) 10x/0.1x heuristic. Much wider than max_value. |

## `food_logs`
_Meal entries (macros, verdict, Viome flags). is_day_summary rows hold a day total, not a meal._

| column | type | meaning / unit / trap |
|---|---|---|
| `id` | uuid | PK. |
| `user_id` | uuid | LEGACY auth.users id — DORMANT. Use profile_id. |
| `meal_type` | text | breakfast \| lunch \| dinner \| snack \| drink \| supplement (CHECK). |
| `description` | text | Meal description (free text). |
| `calories` | numeric | kcal. TRAP: must be parsed with thousands separators stripped ("1,020"→1020), never comma-truncated. |
| `protein_g` | numeric | Protein (g). |
| `carbs_g` | numeric | Carbohydrate (g). |
| `fat_g` | numeric | Fat (g). |
| `fiber_g` | numeric | Fibre (g). |
| `source` | text | Origin: manual_skill \| manual_import \| photo etc. |
| `logged_at` | timestamp with time zone | Legacy timestamp. Prefer log_date + meal_time. |
| `image_url` | text | Source photo URL if any. |
| `notes` | text | Free text. |
| `created_at` | timestamp with time zone | Row insert time. |
| `log_date` | date | Date of the meal (date). |
| `meal_time` | time without time zone | Time of the meal (time). |
| `location` | text | Where eaten. |
| `foods` | jsonb | JSONB array of resolved food items. |
| `verdict` | text | GREEN/AMBER/RED or similar diet-fit verdict. |
| `flags` | text[] | TEXT[] of dietary flags (e.g. Viome avoid/minimize hits). |
| `is_day_summary` | boolean | TRAP: TRUE = a whole-day total row, not a single meal. Exclude from per-meal analysis; use for daily macros. |
| `sprint_id` | uuid | FK sprints.id (auto-tagged). |
| `source_photo_path` | text | Path of the source food photo. |
| `source_log_path` | text | Path of the source log file. Part of the file-sourced upsert key (with log_date, description). |
| `profile_id` | uuid | FK profiles.id — live per-person key. |

## `supplement_regimens`
_A person's supplement protocol (dose/timing/duration, cyclical aware). status: active|discontinued|planned. No general status enum beyond CHECK._

| column | type | meaning / unit / trap |
|---|---|---|
| `id` | uuid | PK. |
| `profile_id` | uuid | FK profiles.id. |
| `supplement_id` | uuid | FK supplements.id. |
| `dose_amount` | numeric | Dose quantity (numeric). |
| `dose_unit` | text | Dose unit (mg, mcg, g, IU, serving...). |
| `timing` | text[] | TEXT[] of timings (wake, breakfast, lunch, dinner, bedtime, anytime). |
| `frequency` | text | daily \| cyclical etc. |
| `cycle_days_on` | integer | Cyclical: days on. |
| `cycle_days_off` | integer | Cyclical: days off. |
| `start_date` | date | Protocol start (from source, never now()). |
| `end_date` | date | Protocol end (NULL = ongoing). |
| `status` | text | active \| discontinued \| planned. |
| `purpose` | text | Why it's taken. |
| `sprint_id` | uuid | FK sprints.id if tied to a training block. |
| `program_id` | uuid | FK training_programs.id if tied to a program. |
| `notes` | text | Free text. |
| `created_at` | timestamp with time zone | Row insert time. |
| `updated_at` | timestamp with time zone | Last update. |

## `supplement_intake_logs`
_Actual supplement intake events. Upsert key (profile_id, supplement_id, taken_on, source). source CHECK: manual|journal|skill|csv._

| column | type | meaning / unit / trap |
|---|---|---|
| `id` | uuid | PK. |
| `profile_id` | uuid | FK profiles.id. |
| `regimen_id` | uuid | FK supplement_regimens.id (auto-attached from active regimen). |
| `supplement_id` | uuid | FK supplements.id. |
| `taken_at` | timestamp with time zone | Intake timestamp (timestamptz). |
| `taken_on` | date | Intake date (date) — part of the upsert key. |
| `dose_amount` | numeric | Dose taken. |
| `dose_unit` | text | Dose unit. |
| `source` | text | CHECK: manual \| journal \| skill \| csv. Default skill. |
| `notes` | text | Free text. |
| `created_at` | timestamp with time zone | Row insert time. |

## `health_context_notes`
_Skill session memory. TRAP: notes are NARRATIVE + references to numbers — NEVER store the numbers themselves (always pull live from the DB). One 'current' note per profile + short 'log' notes._

| column | type | meaning / unit / trap |
|---|---|---|
| `id` | uuid | PK. |
| `profile_id` | uuid | FK profiles.id. |
| `kind` | text | current (single superseding summary per profile) \| log (recent, keep last ~5). |
| `summary` | text | Narrative/state ONLY. No numbers — reference the DB instead. |
| `refs` | jsonb | JSONB pointers to where numbers live (e.g. {"biomarkers":"2026-06-02 DEXA"}). Not the numbers. |
| `s3_keys` | text[] | TEXT[] optional external MD files on S3. |
| `supersedes_id` | uuid | Self-FK: the note this one replaces. |
| `created_at` | timestamp with time zone | Row insert time. |

## `sprints`
_Training blocks (location+date window). TRAP: NO status column — derive active/completed/upcoming from start_date/end_date vs today._

| column | type | meaning / unit / trap |
|---|---|---|
| `id` | uuid | PK. |
| `user_id` | uuid | LEGACY auth.users id — DORMANT. Use profile_id. |
| `name` | text | Sprint name. |
| `slug` | text | Unique slug per profile (upsert key with profile_id). |
| `location` | text | Where the block happens. |
| `start_date` | date | Block start (date). Used for auto-tagging cycles/workouts and deriving status. |
| `end_date` | date | Block end (date). |
| `goals` | jsonb | TEXT[] of goals for the block. |
| `notes` | text | Free text. |
| `created_at` | timestamp with time zone | Row insert time. |
| `profile_id` | uuid | FK profiles.id — live per-person key. |

## `travel_log`
_Trips for location-based performance analysis (feeds v_india_vs_travel). Upsert key (profile_id, start_date, destination)._

| column | type | meaning / unit / trap |
|---|---|---|
| `id` | uuid | PK. |
| `profile_id` | uuid | FK profiles.id. |
| `start_date` | date | Trip start (date). |
| `end_date` | date | Trip end (date). |
| `destination` | text | Destination name. |
| `country_code` | text | ISO country code. |
| `alcohol_level` | text | Subjective alcohol intake during the trip. |
| `activity_context` | text | Trip activity context (e.g. trek, business). |
| `is_altitude` | boolean | TRUE = high-altitude trip (affects recovery/SpO2). |
| `timezone_hint` | text | Timezone hint for the location. |
| `notes` | text | Free text. |
| `created_at` | timestamp with time zone | Row insert time. |

## `wearable_sync_log`
_One row per ingestion run (ground truth for "did it run, what happened"). status CHECK: pending|in_progress|success|failed._

| column | type | meaning / unit / trap |
|---|---|---|
| `id` | uuid | PK. |
| `user_id` | uuid | LEGACY auth.users id — DORMANT. Use profile_id. |
| `provider` | text | whoop, ultrahuman, etc. |
| `sync_type` | text | What synced (whoop_workouts, webhook:workout.updated, etc.). |
| `status` | text | pending \| in_progress \| success \| failed (CHECK). NOT 'running'. |
| `records_synced` | integer | Legacy counter. Prefer records_in/upserted/skipped/failed. |
| `error_message` | text | Run-level error if any. |
| `started_at` | timestamp with time zone | Run start. |
| `completed_at` | timestamp with time zone | Run end. |
| `created_at` | timestamp with time zone | Row insert time. |
| `profile_id` | uuid | FK profiles.id. |
| `method` | text | csv \| screenshot \| photo \| manual \| api (CHECK). |
| `records_in` | integer | Records read from the source. |
| `records_upserted` | integer | Records written (insert+update). |
| `records_skipped` | integer | Records skipped (already present / no match). |
| `records_failed` | integer | Records that errored (see wearable_sync_errors). |
| `document_id` | uuid | FK documents.id if a PDF source. |
| `source_path` | text | Source file/path or descriptor. |

## `wearable_sync_errors`
_Per-record ingestion failures (one row per bad record). Click a failed run to see exactly which rows died and why._

| column | type | meaning / unit / trap |
|---|---|---|
| `id` | uuid | PK. |
| `sync_log_id` | uuid | FK wearable_sync_log.id. |
| `record_ref` | text | Identifier of the failing record. |
| `error_code` | text | Short error category. |
| `error_message` | text | Error detail. |
| `raw` | jsonb | JSONB of the raw record that failed. |
| `created_at` | timestamp with time zone | Row insert time. |
