# Dea — HealthSpan Context
<!-- v2.1 — 2026-06-13 — HR zones updated to %HRmax (max 206), Whoop settings + 2026 program added -->
profile_id: 3eed5503-a26f-4b88-bb76-075208fa5de3   age: 14   sex: F   is_minor: false

## Who & why
13F, growing and active. Primary sports: box-fitness, weightlifting (circuit-style), swimming,
running. Optimising for healthy growth, sleep, energy, and enjoyment of sport — NEVER weight
loss, restriction, or adult performance protocols.

## Measured baselines (from Whoop data, Dec 2024 – Jun 2026)

### Recovery & cardiovascular
- avg_recovery_pct: 63.3       # moderate — should be 70-80 for her age (283 scored cycles)
- avg_resting_hr: 68.0 bpm     # high for a fit teen — target 55-65
- avg_hrv_ms: 50.5             # low for age — healthy teen should be 60-80+
- hrv_range: 9.1–81.8 ms
- rhr_range: 57–104 bpm        # 104 spike Dec 2024 (illness episode)
- spo2_avg: ~96-97%            # normal, healthy

### Training load (146 workouts)
- sessions_above_185bpm: 57/146 (39%)   # TOO HIGH — redlining in ~2 of every 5 sessions
- avg_max_hr_boxfitness: 178 (peak 200)
- avg_max_hr_weightlifting: 187 (peak 201)  # HIGHER than cardio — circuit style, not true strength
- avg_max_hr_swimming: 169 (peak 188)       # most controlled modality
- avg_max_hr_running: 186 (peak 202)

### Concerning patterns
- max_hr_during_weightlifting > max_hr_during_boxfitness (backwards — likely circuit training or sensor artefact from gripping)
- May 17 2026 weightlifting: avg HR 163, 48.8% in Zone 4 — not strength training, this is cardio with weights
- Feb 14-23 2026: 10-day recovery crash (21-37%), RHR 76-82, HRV 23-30 after consecutive high-intensity sessions
- Regular recovery crashes to 20-30% range (Feb 2025, Oct 2025, Dec 2025, Feb 2026)

## Targets / norms
- daily_calories: 2400         # she is GROWING — flag <2400 as LOW, never as "good restraint"
- protein_g: 80 ; sleep_target_h: 9
- resting_hr_target: 60        # currently 68 avg — should improve with aerobic base work
- hrv_target_ms: 65            # currently 50 avg — should improve with less chronic high-intensity
- training_focus: fun, movement, sport skill, AEROBIC BASE
- weekly_active_days_target: 5
- max_sessions_above_185bpm: 2  # per week — currently ~3-4, too many
- weekly_zone2_sessions: 2      # swimming or easy running at 120-145 bpm — ADDING
- biomarker_priorities: hemoglobin, ferritin, vitamin_d   # growth/iron, not cardio-risk panel

## Training recommendations (for PC to implement)
- Distinguish STRENGTH (heavy, 3-5 reps, 2-3 min rest, HR <150) from CIRCUITS (continuous, HR 160+)
- Cap high-intensity sessions (>185 bpm) at 2x/week max
- Add 2x/week genuine Zone 2: swimming or easy running at 120-145 bpm
- Swimming is her best-controlled modality (avg max HR 169 vs 187 weightlifting) — prioritise it
- When HRV drops below 35 on consecutive days → back off, Zone 1-2 only
- Never train hard on two consecutive days with recovery <50%
- The "weightlifting" sessions need restructuring: actual rest between sets (2-3 min), not continuous circuits

## HR zones (%HRmax method — observed max HR 206, Jun 2026)
  # Method: % of max HR (NOT Whoop's default HRR — HRR runs too hot for a teen base)
- zone1: <125 bpm (recovery, easy movement, warm-up, beach walks)
- zone2: 125-150 bpm (aerobic base — THE money zone, where she SHOULD spend more time)
- zone3: 150-170 bpm (tempo — moderate, box-fitness, moderate swims)
- zone4: 170-188 bpm (threshold/high intensity — cap at 2 sessions/week)
- zone5: 188-206 bpm (max — sprint finishers only, currently too frequent)
- note: estimates from wrist HR. A field threshold test or uVida test would sharpen them.

## Whoop app settings (set Jun 2026 to match the plan)
- max_hr: 206  # set first — drives strain & calorie accuracy
- zone_method: MANUAL %HRmax (override Whoop's default HRR, which puts her easy base into tempo)
- manual_zones: Z1 <125 / Z2 125-150 / Z3 150-170 / Z4 170-188 / Z5 188-206
- sleep_goal: "Perform" / 9 h
- strain: use daily recovery-based Strain Target (green push / yellow ease / red Z1-2 or rest)
- weekly_plan: time-in-zone goals weighted to Zones 1-2
- maintenance: manual zones don't auto-adjust — re-check max HR + bands once a term

## Coaching framing
- voice: supportive teen-athlete coach — encouraging, age-appropriate, no jargon
- prescriptiveness: advisory
- is_maintainer: false         # Dea never sees data-quality / audit machinery

## Safety constraints
- NO adult supplement, hormone, or fasting protocols.
- Body-composition / calorie framing: growth & performance ONLY — never deficit, restriction, or "lose" language.
- Treat low intake or skipped meals as a flag to surface gently, never to praise.
- Escalate to a parent/clinician, don't coach, on any red-flag symptom or disordered-eating signal.
- HR above 200 more than 1x/month → flag for clinician review (has happened 3 times in 18 months — borderline).
