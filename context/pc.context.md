# PC — HealthSpan Context
profile_id: 21f69003-46f8-4e1c-a928-b1f694ce4aff   age: 45   sex: M   is_minor: false

<!-- INITIAL DRAFT — PC to refine. The skill reads ALL targets/norms from this file;
     it never substitutes a population default. A missing entry → the skill asks. -->

## Who & why
45M longevity optimiser and the data MAINTAINER for this household. Optimising for
healthspan: insulin sensitivity, cardiovascular risk (ApoB/Lp(a)), VO2max, lean mass
retention, and sleep/recovery consistency across heavy travel.

## Targets / norms
- daily_calories: 2000        # maintenance; flag sustained large surplus/deficit
- protein_g: 160 ; sleep_target_h: 8 ; resting_hr_ceiling: 60
- training_focus: VO2 + Zone2 ; weekly_z4z5_min_target: 12 ; weekly_zone2_min_target: 180
- biomarker_priorities: hba1c, fasting_insulin, apo_b, lpa, hscrp, homocysteine
- recovery_floor_pct: 40       # below this = back off, not push

## Coaching framing
- voice: evidence-based longevity coach — substantive, direct, gives the why
- prescriptiveness: high
- is_maintainer: true          # PC sees ingestion-health + query-audit machinery

## Safety constraints
- Adult protocols permitted (supplements, fasting, hormone optimisation) — PC directs his own.
- Escalate, don't diagnose, on red-flag symptoms (chest pain, syncope, neuro deficit).
