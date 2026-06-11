#!/usr/bin/env python3
"""seed_strength_063.py — one-off seed for PC's 2026-06-11 strength session.

SEPARATE from migration 063 by design: strength_logs is transportable/multi-tenant,
so personal session data must NOT live in a schema migration (it would replay on
every instance). This script seeds only PC's profile.

HELD: the machine_chest_press set recorded load_unit='plates' value 140 — 'plates'
is not a valid load_unit. Not seeded until PC confirms the real unit (likely 140 lb
stack). Flagged back, not guessed.

Run: python3 scripts/seed_strength_063.py [--commit]
Without --commit it prints what it WOULD insert (dry run).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hs_ops  # noqa: E402

PROFILE_ID = "21f69003-46f8-4e1c-a928-b1f694ce4aff"  # PC
PERFORMED_AT = "2026-06-11T13:30:00+07:00"

# (exercise, modality, load_value, load_unit, sets, reps, rir, device_specific, notes)
ROWS = [
    ("trap_bar_deadlift", "barbell", 165, "lb", 5, 5, None, False,
     "breath-limited, legs had more"),
    ("cable_lat_pulldown", "cable", 28, "kg", 2, 6, None, True, None),
    ("cable_lat_pulldown", "cable", 24.5, "kg", 2, 6, None, True,
     "dropped from 28 to hold form"),
]


def main() -> None:
    commit = "--commit" in sys.argv
    hs_ops._load_env()
    conn = hs_ops.connect(readonly=False) if commit else None
    cur = conn.cursor() if conn else None
    for (exercise, modality, load_value, load_unit, sets, reps, rir,
         device_specific, notes) in ROWS:
        if not commit:
            print(f"  DRY    {exercise:20s} {load_value}{load_unit} {sets}x{reps}")
            continue
        cur.execute(
            """INSERT INTO public.strength_logs
                 (profile_id, performed_at, exercise, modality, load_value,
                  load_unit, sets, reps, rir, device_specific, notes, source)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'skill')
               RETURNING id""",
            (PROFILE_ID, PERFORMED_AT, exercise, modality, load_value, load_unit,
             sets, reps, rir, device_specific, notes),
        )
        rid = cur.fetchone()[0]
        print(f"  INSERT {exercise:20s} {load_value}{load_unit} {sets}x{reps} -> {rid}")
    print("  HELD: machine_chest_press (load_unit='plates', value 140) — "
          "confirm real unit before seeding.")
    if commit:
        conn.commit()
        print("Committed.")
    else:
        print("Dry run — re-run with --commit to insert.")


if __name__ == "__main__":
    main()
