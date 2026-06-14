#!/usr/bin/env python3
"""
import_dea_nutrition_tracker.py — one-off import of Dea's spreadsheet daily
nutrition totals into food_logs as `is_day_summary=true` rows.

Source: "Dea's nutrition tracker.xlsx" (Sheet1) — one row per day, columns:
  Date | Calories | Protein(g) | Carbs(g) | Fat(g) | Zinc(mg) | Calcium(mg) | note

food_logs has columns for calories + the 4 macros only; zinc/calcium have no
columns, so they (plus the header targets) are preserved in `foods` jsonb and a
human-readable `notes` string. Day-summary rows are excluded from every per-meal
brief/aggregation, so this is an inert historical archive — it never
double-counts against Dea's live brief.

Dates parse as D\\M\\YY (year 2026). N/A ("didn't track") rows and the dateless
tail rows are SKIPPED and reported. Idempotent: deletes any prior import for this
(profile, source_log_path) before inserting.

Dry-run by default; pass --commit to write.
"""
import os, sys, json, datetime
import openpyxl
import scripts.hs_ops as ops

XLSX = "/Users/p.chitalkar/Downloads/Dea's nutrition tracker.xlsx"
DEA = "3eed5503-a26f-4b88-bb76-075208fa5de3"
SOURCE_TAG = "Dea's nutrition tracker.xlsx"
TARGETS = {"calories": 2730, "protein_g": 135, "carbs_g": 300,
           "fat_g": 115, "zinc_mg": 10, "calcium_mg": 1300}


def parse_date(raw):
    if raw is None:
        return None
    s = str(raw).strip()
    if not s or "N\\A" in s.upper() or "N/A" in s.upper():
        return None
    parts = [p.strip() for p in s.replace("/", "\\").split("\\")]
    if len(parts) != 3:
        return None
    try:
        d, m, y = (int(p) for p in parts)
    except ValueError:
        return None
    y = 2000 + y if y < 100 else y
    try:
        return datetime.date(y, m, d)
    except ValueError:
        return None


def num(v):
    """A cell -> float, or None if it's N/A / blank / a typed sum-expression."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    if not s or "N\\A" in s.upper() or "N/A" in s.upper():
        return None
    return None  # typed expressions like "51+49+..." are left for manual review


def main(commit):
    wb = openpyxl.load_workbook(XLSX, data_only=True)
    ws = wb["Sheet1"]
    rows = list(ws.iter_rows(values_only=True))

    imported, skipped = [], []
    for r in rows[1:]:
        date_cell = r[0]
        d = parse_date(date_cell)
        note_cell = r[7] if len(r) > 7 else None
        note = str(note_cell).strip() if note_cell and str(note_cell).strip() else None
        cals, prot, carb, fat, zinc, calc = (num(r[i]) for i in range(1, 7))

        if d is None:
            label = str(date_cell).strip() if date_cell else "(no date)"
            skipped.append((label, note or "no parseable date / N/A"))
            continue
        if all(x is None for x in (cals, prot, carb, fat, zinc, calc)):
            skipped.append((d.isoformat(), note or "no numeric values"))
            continue

        micros = {}
        if zinc is not None:
            micros["zinc_mg"] = zinc
        if calc is not None:
            micros["calcium_mg"] = calc

        foods = [{
            "kind": "day_summary_import",
            "source_file": SOURCE_TAG,
            "micronutrients_mg": micros,
            "targets": TARGETS,
        }]
        notes_bits = ["Imported from Dea's nutrition tracker spreadsheet (daily total)."]
        if micros:
            notes_bits.append("Zinc %s mg; Calcium %s mg." % (
                micros.get("zinc_mg", "—"), micros.get("calcium_mg", "—")))
        if note:
            notes_bits.append(note.strip("() ").capitalize() + ".")
        notes = " ".join(notes_bits)

        imported.append(dict(
            profile_id=DEA, log_date=d,
            logged_at=datetime.datetime(d.year, d.month, d.day, 12, 0, tzinfo=datetime.timezone.utc),
            description="Daily nutrition total (imported)",
            calories=cals, protein_g=prot, carbs_g=carb, fat_g=fat,
            source="manual_import", is_day_summary=True,
            source_log_path=SOURCE_TAG, foods=json.dumps(foods), notes=notes,
        ))

    print("=== PARSE SUMMARY ===")
    print("Rows to import : %d" % len(imported))
    if imported:
        print("  date range   : %s -> %s" % (imported[0]["log_date"], imported[-1]["log_date"]))
    print("Rows skipped   : %d" % len(skipped))
    for label, why in skipped:
        print("  SKIP %-12s %s" % (label, why))
    print("\nSample row:")
    s = imported[0]
    for k in ("log_date", "calories", "protein_g", "carbs_g", "fat_g", "notes"):
        print("  %-11s %s" % (k, s[k]))

    if not commit:
        print("\n(dry-run) pass --commit to write %d rows." % len(imported))
        return

    ops._load_env()
    import psycopg2, psycopg2.extras
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    try:
        with conn:
            cur = conn.cursor()
            cur.execute(
                "DELETE FROM food_logs WHERE profile_id=%s AND is_day_summary=true "
                "AND source_log_path=%s", (DEA, SOURCE_TAG))
            deleted = cur.rowcount
            psycopg2.extras.execute_values(
                cur,
                "INSERT INTO food_logs (profile_id, log_date, logged_at, description, "
                "calories, protein_g, carbs_g, fat_g, source, is_day_summary, "
                "source_log_path, foods, notes) VALUES %s",
                [(x["profile_id"], x["log_date"], x["logged_at"], x["description"],
                  x["calories"], x["protein_g"], x["carbs_g"], x["fat_g"], x["source"],
                  x["is_day_summary"], x["source_log_path"], x["foods"], x["notes"])
                 for x in imported],
            )
        print("\nCOMMITTED: deleted %d prior import row(s), inserted %d." % (deleted, len(imported)))
    finally:
        conn.close()


if __name__ == "__main__":
    main(commit="--commit" in sys.argv)
