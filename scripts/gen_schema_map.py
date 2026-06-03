#!/usr/bin/env python3
"""gen_schema_map.py — generate docs/SCHEMA-MAP.md from pg_description.

The COMMENT ON column/table entries (migration 016) are the SINGLE source of truth.
This doc is GENERATED, never hand-edited — so it can't drift from the DB. The skill
loads SCHEMA-MAP.md (or queries pg_description live) BEFORE composing any ad-hoc SQL.

Usage: python scripts/gen_schema_map.py   (writes docs/SCHEMA-MAP.md)
"""
import os
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import hs_ops  # noqa: E402

ACTIVE = [
    'profiles', 'families', 'family_memberships',
    'whoop_cycles', 'whoop_sleeps', 'whoop_workouts',
    'workout_intervals', 'workout_hr_samples', 'hr_zone_config',
    'biomarkers', 'metric_definitions',
    'food_logs', 'supplement_regimens', 'supplement_intake_logs',
    'health_context_notes', 'sprints', 'travel_log',
    'wearable_sync_log', 'wearable_sync_errors',
]


def main():
    hs_ops._load_env()
    conn = hs_ops.connect(readonly=True)
    cur = conn.cursor()

    out = ["# HealthSpan — Schema Map (semantic reference)",
           "",
           "> **GENERATED from pg_description (column/table COMMENTs, migration 016). "
           "Do NOT hand-edit — re-run `scripts/gen_schema_map.py`.** "
           "The skill loads this before composing any ad-hoc SQL.",
           f"> Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}.",
           ""]

    for t in ACTIVE:
        # table comment
        cur.execute("SELECT obj_description(('public.'||%s)::regclass)", (t,))
        tbl_comment = (cur.fetchone() or [None])[0] or ""
        out.append(f"## `{t}`")
        if tbl_comment:
            out.append(f"_{tbl_comment}_")
        out.append("")
        out.append("| column | type | meaning / unit / trap |")
        out.append("|---|---|---|")
        cur.execute("""
            SELECT a.attname,
                   format_type(a.atttypid, a.atttypmod) AS typ,
                   col_description(a.attrelid, a.attnum) AS comment
            FROM pg_attribute a
            JOIN pg_class cl ON cl.oid = a.attrelid
            JOIN pg_namespace n ON n.oid = cl.relnamespace
            WHERE n.nspname='public' AND cl.relname=%s
              AND a.attnum > 0 AND NOT a.attisdropped
            ORDER BY a.attnum
        """, (t,))
        for name, typ, comment in cur.fetchall():
            c = (comment or "").replace("|", "\\|")
            out.append(f"| `{name}` | {typ} | {c} |")
        out.append("")

    conn.close()
    docs = os.path.join(ROOT, "docs")
    os.makedirs(docs, exist_ok=True)
    path = os.path.join(docs, "SCHEMA-MAP.md")
    with open(path, "w") as f:
        f.write("\n".join(out))
    print(f"Wrote {path} ({len(ACTIVE)} tables)")


if __name__ == "__main__":
    main()
