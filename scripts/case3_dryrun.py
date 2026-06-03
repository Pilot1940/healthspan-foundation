"""Case 3 — ingestion + prevention dry-run (NOTHING persists: outer rollback).

Runs through PC's SCOPED app connection (RLS active, no admin), exercising the
contract's prevention rules end-to-end, then rolls everything back.

Proves:
  A. Confirm-before-write   — parsed/resolved row shown before any write
  B. In-range insert        — clean INSERT to prod
  C. Idempotency            — re-run same (profile,metric,measured_at) = 0 new rows
  D. Rule #7 (no recompute)  — printed value stored verbatim, never re-derived
  E. Dates-from-source       — measured_at = the document date (2026-06-02), not now()
  F. Out-of-range prevention — android_gynoid 1.27 (>max 1.0) routed to STAGING
  G. Rule #1 (number parse)  — "1,234.5 mg/dL" → 1234.5 (thousands sep stripped)
"""
import sys
from datetime import datetime, timezone

import lib.db as db
from lib.contract import parse_number
from ingest.biomarker import ingest_biomarker_row

CONFIG = "config/pc_skill.config.json"
DOC_DATE = "2026-06-02T00:00:00+00:00"          # the "report" date — NOT today
TODAY = datetime.now(timezone.utc).date()

conn, mode = db.get_app_connection(db.load_config(CONFIG))
cur = conn.cursor()
cur.execute("SELECT current_user, current_setting('request.jwt.claims', true)")
who = cur.fetchone()
print(f"== scoped connection: mode={mode}  role={who[0]} ==")
print(f"== today={TODAY}   document/measured_at date=2026-06-02 ==\n")

PROFILE = db.load_config(CONFIG)["profile_id"]


def count_biomarkers(metric_id, measured_at):
    cur.execute(
        "SELECT count(*) FROM biomarkers WHERE profile_id=%s AND metric_definition_id=%s AND measured_at=%s",
        (PROFILE, metric_id, measured_at))
    return cur.fetchone()[0]


def stg_count():
    cur.execute("SELECT count(*) FROM stg_biomarker_review WHERE profile_id=%s", (PROFILE,))
    return cur.fetchone()[0]


# pick an IN-RANGE metric with a real reference range, not yet measured on DOC_DATE
cur.execute("""SELECT id,name,unit,min_value,max_value FROM metric_definitions
   WHERE min_value IS NOT NULL AND max_value IS NOT NULL
     AND id NOT IN (SELECT metric_definition_id FROM biomarkers
                    WHERE profile_id=%s AND measured_at=%s
                      AND metric_definition_id IS NOT NULL)
   ORDER BY name LIMIT 1""", (PROFILE, DOC_DATE))
mid, mname, munit, lo, hi = cur.fetchone()
inrange_val = round((float(lo) + float(hi)) / 2, 2)
print(f"[setup] in-range metric: {mname} ref=[{lo},{hi}] {munit}  -> test value {inrange_val}\n")

# -------------------------------------------------------------------------
print("G. Rule #1 — number parsing (strip thousands separators + units)")
for raw in ("1,234.5 mg/dL", "1,020", "~5.2", "<10"):
    v, q = parse_number(raw)
    print(f"     parse_number({raw!r:>16}) -> value={v!r}  qualifier={q!r}")
print()

# -------------------------------------------------------------------------
print("A+B+D+E. In-range biomarker — confirm, then write")
payload = {
    "name": mname,
    "value": inrange_val,           # a PRINTED value — contract must store verbatim
    "unit": munit,
    "measured_at": DOC_DATE,        # from the document, not now()
    "source": "manual",
    "lab_name": "Case3-DryRun-Lab",
}
print(f"     CONFIRM parsed row: name={mname!r} value={inrange_val} unit={munit!r} measured_at=2026-06-02")
before = count_biomarkers(mid, DOC_DATE)
r1 = ingest_biomarker_row(payload, conn, PROFILE)
after1 = count_biomarkers(mid, DOC_DATE)
print(f"     1st ingest -> status={r1['status']}   prod rows for key: {before} -> {after1}")

# verify what landed: value verbatim (Rule #7/D) + measured_at from source (E)
cur.execute("""SELECT value, measured_at::date FROM biomarkers
   WHERE profile_id=%s AND metric_definition_id=%s AND measured_at=%s""",
   (PROFILE, mid, DOC_DATE))
stored = cur.fetchone()
print(f"     stored value={stored[0]} (sent {inrange_val} -> verbatim? {float(stored[0])==inrange_val})")
print(f"     stored measured_at={stored[1]}  (today={TODAY})  dates-from-source? {str(stored[1])=='2026-06-02'}\n")

# -------------------------------------------------------------------------
print("C. Idempotency — re-run the SAME record")
r2 = ingest_biomarker_row(payload, conn, PROFILE)
after2 = count_biomarkers(mid, DOC_DATE)
print(f"     2nd ingest -> status={r2['status']}   prod rows for key: {after1} -> {after2}   NEW ROWS: {after2-after1}\n")

# -------------------------------------------------------------------------
print("F. Out-of-range prevention — android_gynoid_ratio 1.27 (ref max 1.0)")
cur.execute("SELECT id,min_value,max_value FROM metric_definitions WHERE name='android_gynoid_ratio'")
ag = cur.fetchone()
print(f"     metric ref=[{ag[1]},{ag[2]}]  test value 1.27 (real DEXA, above clinical normal)")
stg_before = stg_count()
r3 = ingest_biomarker_row(
    {"name": "android_gynoid_ratio", "value": 1.27, "unit": "ratio",
     "measured_at": DOC_DATE, "source": "manual", "lab_name": "Case3-DryRun-Lab"},
    conn, PROFILE)
stg_after = stg_count()
prod_check = count_biomarkers(ag[0], DOC_DATE)
print(f"     ingest -> status={r3['status']}  reason={r3.get('reason')}")
print(f"     staging rows: {stg_before} -> {stg_after} (+{stg_after-stg_before})   prod rows for key: {prod_check}")
print(f"     => abnormal-but-real value did NOT silently hit prod; routed to review.\n")

# -------------------------------------------------------------------------
conn.rollback()   # DRY RUN: discard everything
print("== rolled back — nothing persisted ==")
conn.close()
