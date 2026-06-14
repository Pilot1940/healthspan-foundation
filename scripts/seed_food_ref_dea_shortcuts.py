#!/usr/bin/env python3
"""
seed_food_ref_dea_shortcuts.py — seed Dea's personal food shortcuts into
food_reference (profile-scoped rows) so her frequent meals auto-resolve macros
on log instead of needing clarification.

Personal rows are UNIQUE on (lower(name), profile_id). Idempotent: ON CONFLICT
updates the macros. source='manual', verified=true (PC-curated values).

Dry-run by default; pass --commit to write.
"""
import os, sys
import scripts.hs_ops as ops

DEA = "3eed5503-a26f-4b88-bb76-075208fa5de3"

# (name, calories, protein_g, carbs_g, fat_g) — exact duplicate names collapsed.
SHORTCUTS = [
    ("Cereal + Meiji Choc Milk",            290, 10,   35,   15),
    ("Pad Thai (Minus chives/sprouts)",     460, 23,   68,   11),
    ("C-Vitt Vitamin C Drink",               35,  0,    8,    0),
    ("Pad Thai (Minus chives)",             420, 22,   65,   11),
    ("Green Curry, Rice, Chicken",          460, 24,   55,   18),
    ("Steamed Garlic Broccoli",              35,  2,    5,    1),
    ("Pad Thai (Full portion)",             520, 25,   75,   12),
    ("Scrambled Eggs",                      210, 18,    1,   15),
    ("Croissant Pieces",                    240,  4,   26,   13),
    ("Dutchie Plain Yogurt (Low fat)",       80,  5,   12,  1.5),
    ("Meiji Banana Protein Milk",           260, 33,   23,  3.5),
    ("Pad Thai (Dinner portions)",          750, 35,   95,   24),
    ("Stir-Fried Baby Corn Bowl",            65,  2,    8,    3),
    ("Shake + 1 Scoop Whole Truth",         270, 32,   12,    4),
    ("3 Scrambled Eggs + Ketchup",          230, 18,    3,   15),
    ("1 Whole Banana",                      105,  1,   27,    0),
    ("Fettuccine (Minus bacon/mushrooms)",  380,  8,   45,   18),
    ("Salad (Minus salmon/peas)",            80,  4,   12,    1),
    ("Egg/Avo Sourdough Toast (w/ Parm)",   420, 18,   30,   24),
    ("Plain Yogurt Bowl",                    80,  6,    5,    3),
    ("Choc Muesli + Cashews",               240,  6,   30,   10),
    ("Half a Protein Shake",                130, 16.5, 11.5, 1.7),
    ("PB Banana Shake (w/ Whey)",           460, 35,   42,   17),
    ("Chicken Caesar Croissant (No shell)", 430, 24,   22,   28),
    ("Chicken & Egg Pad Thai",              580, 25,   80,   14),
    ("Chocolate Cereal + Oat Milk",         250,  6,   42,    6),
    ("Coffee Shake (1 Scoop WT + Oat Milk)", 295, 25, 23,   10),
]


def main(commit):
    # guard: no duplicate names in the seed list (would collide on the unique index)
    names = [s[0].lower() for s in SHORTCUTS]
    dupes = {n for n in names if names.count(n) > 1}
    if dupes:
        sys.exit("Duplicate names in seed list: %s" % dupes)

    print("Dea personal food shortcuts to seed: %d" % len(SHORTCUTS))
    for n, c, p, cb, f in SHORTCUTS[:3]:
        print("  e.g. %-34s %4d kcal  P%-5s C%-5s F%s" % (n, c, p, cb, f))
    print("  ...")

    if not commit:
        print("\n(dry-run) pass --commit to write.")
        return

    ops._load_env()
    import psycopg2
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    try:
        with conn:
            cur = conn.cursor()
            for name, cal, prot, carb, fat in SHORTCUTS:
                cur.execute(
                    """INSERT INTO food_reference
                         (name, calories, protein_g, carbs_g, fat_g,
                          profile_id, source, verified, is_active)
                       VALUES (%s,%s,%s,%s,%s,%s,'manual',true,true)
                       ON CONFLICT ((lower(name)), profile_id) WHERE profile_id IS NOT NULL
                       DO UPDATE SET calories=EXCLUDED.calories, protein_g=EXCLUDED.protein_g,
                         carbs_g=EXCLUDED.carbs_g, fat_g=EXCLUDED.fat_g,
                         verified=true, is_active=true, updated_at=now()""",
                    (name, cal, prot, carb, fat, DEA))
        print("COMMITTED %d rows for Dea." % len(SHORTCUTS))
    finally:
        conn.close()


if __name__ == "__main__":
    main(commit="--commit" in sys.argv)
