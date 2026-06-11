#!/usr/bin/env python3
"""seed_food_ref_personal_064.py — PC's personal food_reference rows (mig 064 companion).

SEPARATE from migration 064 by design: a `food_reference` row scoped to a profile is
per-tenant personal data and must not live in a transportable library migration (same
rule as the strength seed, mig 063).

Seeds PC's daily morning shake: "TWT" = The Whole Truth whey protein + creatine +
glutamine + milk. Macros from the explicit full-recipe log (1 scoop TWT + 150 ml
low-fat milk + 6 g creatine + 6 g glutamine = 195 kcal / 30 P / 9 C / 4 F). The
`servings` multiplier still scales partial pours.

Idempotent: re-running updates the row's aliases/macros (matched on lower(name) +
profile_id) rather than duplicating.

Run: python3 scripts/seed_food_ref_personal_064.py [--commit]   (dry run without --commit)
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hs_ops  # noqa: E402

PROFILE_ID = "21f69003-46f8-4e1c-a928-b1f694ce4aff"  # PC

ROW = {
    "name": "Morning TWT Whey Shake (creatine + glutamine)",
    "serving_desc": "1 scoop TWT + 150 ml low-fat milk + 6 g creatine + 6 g glutamine",
    "calories": 195, "protein_g": 30, "carbs_g": 9, "fat_g": 4, "fiber_g": 0,
    "brand": "The Whole Truth",
    "aliases": [
        "twt", "twt shake", "twt protein shake", "the whole truth",
        "the whole truth protein shake", "the whole truth whey",
        "morning protein shake", "morning shake",
        "whey creatine glutamine shake", "morning whey shake",
        "morning protein shake with whey protein, creatine, glutamine, and milk",
    ],
}


def main() -> None:
    commit = "--commit" in sys.argv
    hs_ops._load_env()
    if not commit:
        print(f"  DRY    {ROW['name']} → {ROW['calories']} kcal / {ROW['protein_g']}P "
              f"(profile {PROFILE_ID[:8]}), {len(ROW['aliases'])} aliases")
        print("  Dry run — re-run with --commit to upsert.")
        return
    conn = hs_ops.connect(readonly=False)
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO public.food_reference
             (name, serving_desc, calories, protein_g, carbs_g, fat_g, fiber_g,
              brand, profile_id, source, verified, is_active, aliases)
           VALUES (%(name)s,%(serving_desc)s,%(calories)s,%(protein_g)s,%(carbs_g)s,
                   %(fat_g)s,%(fiber_g)s,%(brand)s,%(pid)s,'seed',true,true,%(aliases)s)
           ON CONFLICT (lower(name), profile_id) WHERE profile_id IS NOT NULL
           DO UPDATE SET serving_desc=EXCLUDED.serving_desc, calories=EXCLUDED.calories,
                         protein_g=EXCLUDED.protein_g, carbs_g=EXCLUDED.carbs_g,
                         fat_g=EXCLUDED.fat_g, fiber_g=EXCLUDED.fiber_g,
                         brand=EXCLUDED.brand, aliases=EXCLUDED.aliases,
                         verified=true, updated_at=now()
           RETURNING id""",
        {**ROW, "pid": PROFILE_ID, "aliases": ROW["aliases"]},
    )
    rid = cur.fetchone()[0]
    conn.commit()
    print(f"  UPSERT {ROW['name']} -> {rid}")
    print("Committed.")


if __name__ == "__main__":
    main()
