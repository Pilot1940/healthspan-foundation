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

# Curated reading order for the high-value tables. This is ONLY an ordering hint:
# the body is driven by the DB (every documented base table is rendered, in this
# order first, then the rest alphabetically). A table can never be silently hidden
# by being absent from this list — if it has a comment it renders; if it has none
# it is flagged under "## Unmapped tables". (migration 016 + 016c.)
PREFERRED_ORDER = [
    'profiles', 'families', 'family_memberships',
    'whoop_cycles', 'whoop_sleeps', 'whoop_workouts',
    'workout_intervals', 'workout_hr_samples', 'hr_zone_config',
    'biomarkers', 'metric_definitions',
    'food_logs', 'supplement_regimens', 'supplement_intake_logs',
    'health_context_notes', 'sprints', 'travel_log',
    'wearable_sync_log', 'wearable_sync_errors',
]


def _render_table(cur, out, t):
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


def main():
    hs_ops._load_env()
    conn = hs_ops.connect(readonly=True)
    cur = conn.cursor()

    # Every public BASE table (relkind='r'), with a documented flag. A table is
    # "documented" if it carries a table comment OR at least one column comment.
    # Scope is intentionally base tables only — views (e.g. whoop_journal) are not
    # part of this completeness guarantee, consistent with the original map.
    cur.execute("""
        SELECT c.relname,
               (obj_description(c.oid) IS NOT NULL
                OR EXISTS (SELECT 1 FROM pg_attribute a
                           WHERE a.attrelid = c.oid AND a.attnum > 0
                             AND NOT a.attisdropped
                             AND col_description(c.oid, a.attnum) IS NOT NULL)) AS documented
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relkind = 'r'
        ORDER BY c.relname
    """)
    all_tables = cur.fetchall()
    documented = [t for t, doc in all_tables if doc]
    unmapped = [t for t, doc in all_tables if not doc]

    # render order: preferred high-value tables first, then the rest alphabetically
    ordered = [t for t in PREFERRED_ORDER if t in documented]
    ordered += [t for t in documented if t not in PREFERRED_ORDER]  # already sorted

    now = datetime.now(timezone.utc)
    out = ["# HealthSpan — Schema Map (semantic reference)",
           "",
           "> **GENERATED from pg_description (column/table COMMENTs, migrations 016/016b/016c). "
           "Do NOT hand-edit — re-run `scripts/gen_schema_map.py`.** "
           "The skill loads this before composing any ad-hoc SQL.",
           f"> Generated {now.strftime('%Y-%m-%d %H:%M UTC')}.",
           # machine-parseable stamp — self_test.py WARNs if this is >30 days old
           f"> generated_at: {now.strftime('%Y-%m-%dT%H:%M:%SZ')}",
           f"> coverage: {len(documented)} of {len(all_tables)} public base tables documented.",
           ""]

    for t in ordered:
        _render_table(cur, out, t)

    # Completeness backstop: any live base table with NO comments at all is listed
    # here so the map can never silently hide a table. Empty section = full coverage.
    out.append("## Unmapped tables")
    out.append("")
    if unmapped:
        out.append("> ⚠ These live tables have NO table/column COMMENTs and are therefore "
                   "undocumented. Add them in a `COMMENT ON` migration (see 016/016c) so they "
                   "stop hiding here.")
        out.append("")
        for t in unmapped:
            out.append(f"- `{t}`")
    else:
        out.append("_None — every public base table carries a comment._")
    out.append("")

    conn.close()
    docs = os.path.join(ROOT, "docs")
    os.makedirs(docs, exist_ok=True)
    path = os.path.join(docs, "SCHEMA-MAP.md")
    with open(path, "w") as f:
        f.write("\n".join(out))
    print(f"Wrote {path} ({len(documented)}/{len(all_tables)} tables documented, "
          f"{len(unmapped)} unmapped)")


if __name__ == "__main__":
    main()
