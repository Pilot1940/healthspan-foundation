#!/usr/bin/env python3
"""
seed_food_ref_dea_unlabeled.py — the rows from screenshots 2 & 4 that had FULL
macros but NO visible item name. Per PC's "add it all" + "make assumptions",
names are INFERRED from the macro signature and marked verified=false so they
surface for renaming. Macros are exactly as shown.

Skipped: a 330/10/65/3 row that is a near-duplicate of the existing
'Dal & Rice (Standard portion)' (350/10/65/6).

ON CONFLICT DO NOTHING. Dry-run by default; pass --commit to write.
"""
import os, sys
import scripts.hs_ops as ops

DEA = "3eed5503-a26f-4b88-bb76-075208fa5de3"

# (name [inferred], calories, protein_g, carbs_g, fat_g) — macros verbatim from the shot.
ITEMS = [
    ("Cereal Bowl (unconfirmed)",          150,  6, 28,  2),   # img2 r1
    ("Paneer Bhurji / Egg side (unconfirmed)", 190, 9, 12, 14),# img2 r2: high-fat low-carb
    ("Whey Protein Shake 35g (unconfirmed)", 200, 35, 0,  6),  # img4 r1: 35P/0C = shake
    ("Small Chutney / Side (unconfirmed)",  70,  1,  5,  5),   # img4 r3
]


def main(commit):
    print("Dea unlabeled rows (inferred names, verified=false): %d" % len(ITEMS))
    for n, c, p, cb, f in ITEMS:
        print("  %-40s %4d kcal  P%-3s C%-3s F%s" % (n, c, p, cb, f))
    print("Skipped: 330/10/65/3 (dupe of 'Dal & Rice (Standard portion)').")

    if not commit:
        print("\n(dry-run) pass --commit to write.")
        return

    ops._load_env()
    import psycopg2
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    ins = 0
    try:
        with conn:
            cur = conn.cursor()
            for name, cal, prot, carb, fat in ITEMS:
                cur.execute(
                    """INSERT INTO food_reference
                         (name, calories, protein_g, carbs_g, fat_g,
                          profile_id, source, verified, is_active)
                       VALUES (%s,%s,%s,%s,%s,%s,'screenshot_import',false,true)
                       ON CONFLICT ((lower(name)), profile_id) WHERE profile_id IS NOT NULL
                       DO NOTHING""",
                    (name, cal, prot, carb, fat, DEA))
                ins += cur.rowcount
        print("\nCOMMITTED: %d inserted." % ins)
    finally:
        conn.close()


if __name__ == "__main__":
    main(commit="--commit" in sys.argv)
