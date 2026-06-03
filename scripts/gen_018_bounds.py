"""Generate migration 018 — plausibility bounds for ALL in-use metrics.

Hand-authored WIDE physiological extremes (never flag a real value; catch a
10-100x OCR/unit slip). This generator is the safety net for 127 hand bounds:

  COVERAGE  — the bounds dict must cover EXACTLY the set of in-use, still-unbounded
              metrics (no gaps, no strays). Fails loudly otherwise.
  CONTAINMENT — every value actually stored in biomarkers for a metric must sit
              STRICTLY inside its new [plausible_min, plausible_max], with headroom.
              A violation means the bound is too tight or a unit mismatch — exactly
              the failure mode that would gate a real value on next ingest.

Reads the service-role DATABASE_URL from .env (global view, not RLS-scoped) so it
sees the table the way the migration will. Read-only. Emits the .sql to stdout
path; does NOT apply it.
"""
import os, sys, psycopg2

# (plausible_min, plausible_max) — wide physiological extremes, by metric name.
BOUNDS = {
  # blood_count
  "basophils_abs": (0, 5), "basophils_pct": (0, 100),
  "eosinophils_abs": (0, 30), "eosinophils_pct": (0, 100),
  "hemoglobin": (3, 25), "lymphocytes_abs": (0, 200), "lymphocytes_pct": (0, 100),
  "mch": (5, 60), "mchc": (15, 50), "mcv": (40, 160), "mentzer_index": (0, 100),
  "monocytes_abs": (0, 30), "monocytes_pct": (0, 100), "mpv": (3, 25),
  "neutrophils_abs": (0, 300), "neutrophils_pct": (0, 100), "pcv": (10, 70),
  "platelet_count": (1, 5000), "rbc": (1, 10), "rdw": (5, 70),
  "tlc": (0, 1000), "wbc": (0, 1000),
  # body_composition
  "arms_fat_pct": (0.5, 80), "bmc_kg": (0.1, 10), "bmi": (8, 100),
  "fat_free_mass_kg": (5, 150), "fat_mass_kg": (0.5, 200), "legs_fat_pct": (0.5, 80),
  "subcutaneous_fat_area": (1, 3000), "trunk_fat_pct": (0.5, 80),
  "visceral_fat_area_ct": (1, 2000), "visceral_fat_mass": (1, 20000),
  # bone_density
  "bmc_total": (100, 6000), "bmd_femur_mean": (0.1, 3.0),
  "bmd_femur_neck_left": (0.1, 3.0), "bmd_femur_neck_right": (0.1, 3.0),
  "bmd_femur_total_left": (0.1, 3.0), "bmd_femur_total_right": (0.1, 3.0),
  "bmd_spine_l1_l4": (0.1, 3.0), "bmd_total_body": (0.1, 3.0),
  "tscore_femur_mean": (-10, 10), "tscore_femur_neck_left": (-10, 10),
  "tscore_femur_neck_right": (-10, 10), "tscore_femur_total_left": (-10, 10),
  "tscore_femur_total_right": (-10, 10), "tscore_spine_l1_l4": (-10, 10),
  "tscore_total_body": (-10, 10),
  # cancer_markers (cancer values run extreme — bound very wide)
  "afp": (0, 1000000), "cea": (0, 1000000), "psa": (0, 100000),
  # cardiac
  "coronary_calcium_score": (0, 20000), "diastolic_bp": (20, 200),
  "lung_age": (1, 150), "resting_hr_spot": (20, 300), "systolic_bp": (40, 300),
  "vo2max_estimated": (3, 100), "vo2max_lab": (3, 100),
  # enzymes (rhabdo/pancreatitis run extreme)
  "amylase": (1, 100000), "ck": (1, 1000000), "g6pd": (0, 100),
  "ldh": (10, 100000), "lipase": (1, 100000),
  # hormones
  "cortisol_am": (0.1, 300), "dheas": (1, 3000), "free_t3": (0.1, 50),
  "free_t4": (0.05, 30), "fsh": (0.1, 1000), "igf1": (5, 3000), "lh": (0.1, 1000),
  "prolactin": (0.5, 100000), "pth_intact": (1, 10000), "shbg": (1, 1000),
  "tsh": (0.001, 150),
  # inflammation
  "anti_hbs_titre": (0, 100000), "anti_hcv": (0, 10000), "crp": (0, 1000),
  "esr": (0, 200), "hbsag": (0, 10000), "homocysteine": (1, 500),
  "hscrp": (0, 1000), "ige": (0, 1000000), "rheumatoid_factor": (0, 100000),
  # kidney
  "albumin_urine": (0, 100000), "bun": (1, 500), "bun_creatinine_ratio": (0.5, 200),
  "creatinine": (0.1, 25), "creatinine_urine": (0.5, 5000), "egfr": (1, 250),
  "urea": (1, 1000), "uric_acid": (0.1, 30), "urine_acr": (0, 100000),
  "urine_ph": (4, 9.5), "urine_specific_gravity": (1.0, 1.07),
  # lipids
  "apo_a1": (10, 500), "apo_b": (5, 600), "apo_b_a1_ratio": (0.01, 10),
  "lpa": (0, 2000), "total_cholesterol": (20, 1000),
  # liver
  "albumin_globulin_ratio": (0.1, 20), "alp": (5, 10000), "alt": (1, 50000),
  "ast": (1, 50000), "ast_alt_ratio": (0.05, 50), "bilirubin": (0.05, 100),
  "bilirubin_direct": (0.01, 80), "bilirubin_indirect": (0.01, 80),
  "ggt": (1, 20000), "globulin": (0.5, 15),
  # metabolic
  "eag": (20, 800), "homa_ir": (0.05, 200),
  # minerals
  "bicarbonate": (3, 60), "calcium": (3, 25), "chloride": (50, 160),
  "ferritin": (0, 200000), "iron": (1, 2000), "magnesium_serum": (0.3, 15),
  "phosphorus": (0.3, 25), "potassium": (1, 12), "sodium": (90, 200),
  "tibc": (50, 1500), "transferrin_saturation": (0, 150), "uibc": (5, 1500),
  "zinc": (5, 2000),
  # proteins
  "albumin": (0.5, 8), "total_protein": (1, 15),
  # vitamins (B12 runs very high with supplementation/liver disease)
  "folate": (0.1, 100), "vitamin_b12": (30, 200000),
}


def _connect():
    with open(".env") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k, v.strip())
    c = psycopg2.connect(os.environ["DATABASE_URL"])
    c.set_session(readonly=True)
    return c


def main():
    conn = _connect()
    cur = conn.cursor()

    # in-use AND still-unbounded (global, service-role)
    cur.execute("""
      SELECT md.name, min(b.value), max(b.value), count(b.id)
      FROM metric_definitions md
      JOIN biomarkers b ON b.metric_definition_id = md.id AND b.value IS NOT NULL
      WHERE md.plausible_min IS NULL AND md.plausible_max IS NULL
      GROUP BY md.name
    """)
    live = {r[0]: (float(r[1]), float(r[2]), r[3]) for r in cur.fetchall()}
    conn.close()

    live_names = set(live)
    dict_names = set(BOUNDS)

    # --- COVERAGE -----------------------------------------------------------
    missing = live_names - dict_names       # in use, unbounded, no bound authored
    strays = dict_names - live_names         # authored but not in the in-use set
    if missing:
        sys.exit(f"COVERAGE FAIL — {len(missing)} in-use metrics have NO bound: {sorted(missing)}")
    if strays:
        sys.exit(f"COVERAGE FAIL — {len(strays)} authored bounds are not in the in-use set: {sorted(strays)}")

    # --- CONTAINMENT --------------------------------------------------------
    # Invariant: no real value may be GATED → value must satisfy pmin <= v <= pmax
    # (gating is value < pmin OR value > pmax, so equality at a bound is safe).
    # The min side is INCLUSIVE — many metrics have a true physiological floor of 0
    # (calcium score, hbsag index, differential counts) and observed 0 == pmin is
    # correct. The max side keeps STRICT headroom (omax < pmax): a too-tight ceiling
    # is the real risk, since OCR/unit slips inflate values and a real value already
    # near the ceiling means the bound would gate the next legitimate high reading.
    violations = []
    for name, (omin, omax, n) in sorted(live.items()):
        pmin, pmax = BOUNDS[name]
        if not (pmin <= omin and omax < pmax):
            violations.append(f"{name}: observed [{omin}, {omax}] (n={n}) NOT inside bound [{pmin}, {pmax}] (need pmin<=omin and omax<pmax)")
    if violations:
        sys.exit("CONTAINMENT FAIL — bound too tight / unit mismatch:\n  " + "\n  ".join(violations))

    print(f"-- OK: {len(live)} in-use unbounded metrics, all bounds cover observed data with headroom",
          file=sys.stderr)

    # --- EMIT SQL -----------------------------------------------------------
    rows = []
    for name in sorted(BOUNDS):
        pmin, pmax = BOUNDS[name]
        rows.append(f"  ('{name}', {pmin}, {pmax})")
    values = ",\n".join(rows)

    sql = f"""-- =============================================================
-- 018_implausibility_bounds_full.sql  (ADDITIVE — UPDATE only)
-- Extend the implausibility BOUND (migration 017) to EVERY in-use metric.
-- Generated by scripts/gen_018_bounds.py, which asserts against live data:
--   * COVERAGE   — bounds cover exactly the {len(live)} in-use, still-unbounded metrics.
--   * CONTAINMENT — every value stored in biomarkers sits STRICTLY inside its new
--                   [plausible_min, plausible_max] with headroom (no real value gated).
-- Bounds are WIDE physiological extremes: a real (even pathological) value passes;
-- a 10-100x OCR/unit/comma slip is caught and routed to staging. These are NOT
-- clinical reference ranges (those stay in min_value/max_value and only flag).
-- =============================================================
BEGIN;

UPDATE public.metric_definitions AS m
SET    plausible_min = v.pmin, plausible_max = v.pmax
FROM (VALUES
{values}
) AS v(name, pmin, pmax)
WHERE m.name = v.name
  AND m.plausible_min IS NULL AND m.plausible_max IS NULL;   -- don't clobber 017's bounds

DO $$
DECLARE
    n_unbounded_inuse int;
BEGIN
    SELECT count(*) INTO n_unbounded_inuse
    FROM public.metric_definitions md
    WHERE md.plausible_min IS NULL AND md.plausible_max IS NULL
      AND EXISTS (SELECT 1 FROM public.biomarkers b WHERE b.metric_definition_id = md.id);
    RAISE NOTICE '018: in-use metrics still UNBOUNDED after this migration: % (expect 0)', n_unbounded_inuse;
    IF n_unbounded_inuse <> 0 THEN
        RAISE EXCEPTION '018: % in-use metrics remain unbounded — aborting', n_unbounded_inuse;
    END IF;
END $$;

COMMIT;
"""
    with open("migrations/018_implausibility_bounds_full.sql", "w") as f:
        f.write(sql)
    print("migrations/018_implausibility_bounds_full.sql", file=sys.stderr)


if __name__ == "__main__":
    main()
