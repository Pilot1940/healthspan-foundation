# PC — HealthSpan Context
<!-- v2.2 — 2026-06-07 — Jun 5 Metropolis panels ingested: insulin 28.3 (↓25%), HbA1c 5.6%,
     fasting glucose 102.71, HsCRP 0.65, Vit D 22.2 (dropping), testosterone 391/20.3,
     prolactin 28.4 (2x ULN — needs workup), B12 656, Mg 2.18, cortisol 15.1.
     Supplement-hold gate CLEARED. -->
<!-- v2.1 — 2026-06-04 — 300-Day Plan v3.1: weight 83 (stretch 80), day-type calorie split,
     VO2 45 long-term / 48 Mera stretch, peptides guardrail, active-PF + bone-stress watch. -->
<!-- v2.0 — 2026-06-03 — Updated with DEXA (Jun 2), uVida VO2max + Food (Jun 3) lab results -->
profile_id: 21f69003-46f8-4e1c-a928-b1f694ce4aff   age: 45   sex: M   is_minor: false

## Who & why
45M longevity optimiser and the data MAINTAINER for this household. Optimising for
healthspan: insulin sensitivity, cardiovascular risk (ApoB/Lp(a)), VO2max, lean mass
gain, fat loss (especially visceral), and sleep/recovery consistency across heavy travel.
Active 300-day plan (Jun 2026 → Apr 2027) targeting Mera Peak 6,476M summit.

## Measured baselines (Jun 2–3, 2026)

### DEXA — Miracles Healthcare, Gurugram (Jun 2)
- weight_kg: 100.3 (scale 101) ; height_cm: 179 ; bmi: 31.5
- body_fat_pct: 39.2 ; lean_mass_kg: 59.1 ; fat_mass_kg: 38.2
- fat_free_mass_kg: 62.1 ; bmc_kg: 3.0 (2997g)
- android_fat_pct: 53.8 ; gynoid_fat_pct: 42.2 ; ag_ratio: 1.27
- trunk_fat_pct: 45.0 ; arms_fat_pct: 31.8 ; legs_fat_pct: 36.4
- bmd_femur_neck_right: 0.884 (T -1.4) ; bmd_femur_neck_left: 0.907 (T -1.3)
- bmd_spine_l1l4: 1.216 (T 0.0) ; bmd_total_body: 1.327 (T +1.3)
- note: femur neck T-scores at osteopenia boundary — Vit D (25 ng/mL) likely driver

### uVida Activity — Rana Chengappa, Delhi (Jun 3)
- vo2max_relative: 29.9 ml/min/kg (SUBMAXIMAL — shin pain limited, true value likely 32-35)
- vo2max_absolute: 3.0 L/min
- max_hr_lab: 182 ; iaat_hr: 168 ; iaat_speed_kmh: 8.4
- vt1_hr: 147 ; vt1_speed_kmh: 7.2
- vt2_hr: 168 ; vt2_speed_kmh: 8.4
- fatmax_hr: 110 ; fatmax_speed_kmh: 6.0
- hr_recovery_3min: 38 bpm (Level 2/6)
- note: right outer shin (peroneal) pain at 9+ km/h — XERO minimalist shoes unsuitable at 101kg

### uVida Food — Rana Chengappa, Delhi (Jun 3)
- bmr_measured: 1935 kcal (vs 2034 expected — 5% below, consistent with low lean mass)
- min_energy_need: 2514 kcal (BMR 1935 + Work 416 + Sports 163)
- metabolic_fat_pct: 27 ; metabolic_carb_pct: 58 ; metabolic_protein_pct: 15
- metabolic_flexibility: very_low
- respiratory_rate_rest: 24 breaths/min (target <18)

### Previous baseline comparison (Jul 2023 → Jun 2026)
- body_fat: 42.6% → 39.2% (↓3.4pp) ; lean_mass: 55.8 → 59.1 kg (↑3.3)
- fat_mass: 43.4 → 38.2 kg (↓5.2) ; android_fat: 48.7% → 53.8% (↑5.1pp — worse)
- ag_ratio: 1.11 → 1.27 (↑ — worse) ; bmc: 2655 → 2997g (↑342g)
- vo2max: 29.0 → 29.9 (flat over 3 years)

## Targets / norms
- maintenance_kcal: 2514        # uVida TDEE — reference only (BMR 1935 + Work 416 + Sports 163)
- daily_calories: 2100          # WEEKLY-AVG target (deliberate deficit ~414 below TDEE)
- daily_calories_hard: 2250 ; daily_calories_z2: 2050 ; daily_calories_rest: 1950
  # calorie split by DAY-TYPE: 2250 hard/strength · 2050 Zone-2 · 1950 rest → avg ~2100
  # food deficit: ~414 kcal/day = ~2,900/week → ~0.38 kg/week from diet alone
  # training: 4× ~600 kcal sessions/week = ~2,400/week + walking/NEAT
  # combined realistic weekly deficit: ~5,300–5,800 kcal → ~0.55–0.6 kg/week
  # expect ~0.5 kg/week blended (training weeks higher, rest/travel weeks lower)
  # TAPER: move to MAINTENANCE (~2514) for the ~3-4 weeks pre-expedition AND through the trek —
  #        never trek in a deficit (see Safety constraints).
- calorie_floor: 1935           # BMR — never sustain below
- protein_g: 180 ; sleep_target_h: 8 ; resting_hr_ceiling: 62
- weight_target_kg: 83 ; body_fat_target_pct: 20 ; lean_mass_target_kg: 65
  # weight 83 = realistic 44-week / expedition endpoint; 80 = long-term STRETCH (was 80)
  # lean_mass 65 is a STRETCH: +6kg lean during a continuous deficit → expect preserve→slight gain, NOT assumed
- android_fat_target_pct: 40 ; ag_ratio_target: 1.0
- vo2max_target_jan2027: 40     # Mera go/no-go checkpoint — realistic with weight loss + training
- vo2max_target_longterm: 45    # 2+ year horizon
- vo2max_mera_stretch: 48       # Mera-summit stretch; 50 is aspirational, not a coaching benchmark
- training_focus: VO2max + Zone2 + metabolic_flexibility
- weekly_z4z5_min_target: 12   # AT zone: 168-176 bpm (lab-calibrated)
- weekly_zone2_min_target: 180 # ET1: 110-147 bpm (up to VT1)
- weekly_fatmax_sessions: 3    # fasted morning at 110 bpm / 6.0 km/h, 30-45 min
- biomarker_priorities: fasting_insulin, hba1c, apo_b, lpa, hscrp, homocysteine, vitamin_d
- recovery_floor_pct: 40       # below this = back off, not push
- hrv_target_ms: 50            # from ~33 baseline

## HR zones (lab-calibrated — uVida Jun 3, Whoop manual mode)
- zone1: 110-128 bpm (recovery)
- zone2: 129-147 bpm (ET1 — up to VT1; fatmax at 110 bpm)
- zone3: 148-168 bpm (ET2 — VT1 to VT2/IAAT)
- zone4: 169-176 bpm (AT — threshold training, 4×4 intervals here)
- zone5: 177-182 bpm (CA — max capacity)
- zone_source: uvida_lab ; zone_effective_date: 2026-06-03
- next_retest: 2026-08-26

## Active conditions / contraindications
- insulin_resistance: fasting insulin 28.3 µIU/mL (Jun 5, 2026) — ↓25% from 37.7 Mar; still HIGH (ref 2-25). On Berberine 1500mg/day. HbA1c 5.6%, FG 102.71 — approaching prediabetic.
- prolactin_elevated: 28.4 ng/mL (Jun 5) — persistent ~2x ULN (ref 3-14.7). Was 27.1 Mar. Needs physician workup (MRI pituitary / medication review).
- l4l5_facet_injury: Mar 2026 — Phase 1 recovery, no spinal loading
- shin_pain_peroneal: right outer shin, gradual onset at 9+ km/h treadmill (Jun 3)
- plantar_fasciitis: ACTIVE — daily calf-eccentric + windlass stretch; physio review; PF must trend down within ~4 weeks or CUT XERO volume
- footwear: XERO + Superfeet Green arch insole for walking; Altra Torin 8 for training deload; no treadmill running until shin pain resolved
- bone_stress_watch: shin pain + Vit D 25 ng/mL + femur-neck T -1.3/-1.4 + loaded hiking = elevated bone-stress risk — do NOT progress pack weight through shin pain
- vitamin_d_insufficient: 22.2 ng/mL (Jun 5, 2026) — DROPPING despite D3 6200 IU/day (was 25 Mar, 11.8 Feb). Absorption issue likely — consider K2/fat co-administration or higher dose.
- altitude_profile: slow acclimatiser, AMS at 4070M (May 2026), SpO2 85% at altitude

## Coaching framing
- voice: evidence-based longevity coach — substantive, direct, gives the why
- prescriptiveness: high
- is_maintainer: true          # PC sees ingestion-health + query-audit machinery

## Safety constraints
- Adult protocols permitted (supplements, fasting, hormone optimisation) — PC directs his own.
- Escalate, don't diagnose, on red-flag symptoms (chest pain, syncope, neuro deficit).
- Caloric floor: never recommend eating below BMR 1935 kcal consistently.
- Never program a caloric deficit during the pre-expedition taper (~3-4 weeks out) or the trek — taper to MAINTENANCE (~2514); never trek in a deficit.
- peptides: PHYSICIAN-ONLY — unapproved (FDA safety-concern), no human RCTs, WADA-banned; never self-source or inject. Coach role: redirect to a doctor, never advise dosing.
- Supplement protocol changes: Jun-5 panel NOW AVAILABLE (gate cleared 2026-06-07). Adjustments can proceed based on results.
- Running contraindicated until shin pain resolved — substitute cycling/swimming for VO2max intervals.
