#!/usr/bin/env python3
"""
seed_food_ref_dea_screenshots.py — batch 2 of Dea's personal food shortcuts,
extracted from 11 meal-breakdown screenshots (2026-06-13).

Distinct individual items only (TOTAL/aggregate rows and dupes of the batch-1
shortcuts are excluded).

FAT was not shown in the screenshots (Fats column scrolled off). Per PC's
go-ahead to make assumptions, fat_g is ESTIMATED from the stated calories:
fat ≈ (cal − 4·protein − 4·carbs)/9, sanity-checked against the actual food.
verified=true (PC-accepted estimates). The two items that were also missing
carbs (Agápi yogurt, TWT PB sandwich) are estimated in full.

NOT included: the two screenshots whose item names were cut off (images 2 & 4) —
naming a food from bare macros would be fabrication, not estimation.

ON CONFLICT DO UPDATE so re-runs backfill fat / refresh estimates on rows that
an earlier (fat=0) run already inserted.
Dry-run by default; pass --commit to write.
"""
import os, sys
import scripts.hs_ops as ops

DEA = "3eed5503-a26f-4b88-bb76-075208fa5de3"

# (name, calories, protein_g, carbs_g, fat_g) — fat estimated from calories.
ITEMS = [
    ("Palak Dal Chawal",                        480, 16,  85,  8),
    ("PB&J Sandwich (2 slices)",                320, 10,  45, 11),
    ("Monkfruit Protein Shake",                 140, 25,   5,  2),
    ("Lauki Dal & Rice",                        325, 10,  52,  9),
    ("Noodles with 2 Eggs",                     480, 18,  55, 21),
    ("250ml Cow's Milk (1 cup)",                130,  8,  12,  6),
    ("1 Scoop Monkfruit Protein",               140, 25,   3,  3),
    ("4 Slices Pepperoni Pizza",               1000, 40, 100, 44),
    ("Dal, Rice & Beans",                       350, 12,  60,  7),
    ("Epigamia Greek Yogurt",                    90,  8,   5,  3),
    ("Yoga Bar Muesli (~50g)",                  204,  6,  35,  5),
    ("Rajma Chawal (2 small plates)",           650, 28, 127,  6),
    ("Extra Paneer Cubes (~6-8 pieces)",        260, 18,   3, 20),
    ("Dal & Rice (Standard portion)",           350, 10,  65,  6),
    ("Paneer Cubes (~50g)",                     160,  9,   2, 13),
    ("Shakar / Jaggery (~1.5 tbsp)",             60,  0,  15,  0),
    ("Paneer Paratha (1 med)",                  280, 10,  35, 11),
    ("Chana Poori (1 poori + chana)",           350, 10,  45, 14),
    ("Extra Bowl of Chana + Paneer side",       300, 18,  25, 14),
    ("Boondi Raita (1 bowl)",                   120,  4,  10,  7),
    ("2 TWT Bars (Almond Cocoa)",               330, 10,  30, 16),
    ("Yoga Bar Muesli (Dark Choc/Almond ~40g)", 160,  4,  26,  5),
    # were skipped (no carbs/fat shown) — now fully estimated per PC's go-ahead
    ("Agapi Greek Yogurt (400g)",               280, 36,  14,  9),
    ("The Whole Truth PB Sandwich",             400, 14,  40, 20),
]


def main(commit):
    names = [s[0].lower() for s in ITEMS]
    dupes = {n for n in names if names.count(n) > 1}
    if dupes:
        sys.exit("Duplicate names in seed list: %s" % dupes)

    print("Dea screenshot food shortcuts (batch 2, fat-estimated): %d" % len(ITEMS))
    for n, c, p, cb, f in ITEMS[:4]:
        print("  e.g. %-34s %4d kcal  P%-3s C%-4s F%s(est)" % (n, c, p, cb, f))
    print("  ...")
    print("NOT seeded: images 2 & 4 (item names cut off — unnameable from bare macros);"
          " 3 dupes of batch-1; TOTAL rows.")

    if not commit:
        print("\n(dry-run) pass --commit to write.")
        return

    ops._load_env()
    import psycopg2
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    try:
        with conn:
            cur = conn.cursor()
            for name, cal, prot, carb, fat in ITEMS:
                cur.execute(
                    """INSERT INTO food_reference
                         (name, calories, protein_g, carbs_g, fat_g,
                          profile_id, source, verified, is_active)
                       VALUES (%s,%s,%s,%s,%s,%s,'screenshot_import',true,true)
                       ON CONFLICT ((lower(name)), profile_id) WHERE profile_id IS NOT NULL
                       DO UPDATE SET calories=EXCLUDED.calories, protein_g=EXCLUDED.protein_g,
                         carbs_g=EXCLUDED.carbs_g, fat_g=EXCLUDED.fat_g,
                         verified=true, is_active=true, updated_at=now()""",
                    (name, cal, prot, carb, fat, DEA))
        print("\nCOMMITTED %d rows (insert or fat-backfill)." % len(ITEMS))
    finally:
        conn.close()


if __name__ == "__main__":
    main(commit="--commit" in sys.argv)
