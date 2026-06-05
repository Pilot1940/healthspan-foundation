"""Dry-run harness for migration 016c (schema-comment completion).
Applies 016c in ONE transaction, proves the end state (every base table documented,
profiles 12/12, the generated-col + dormant comments land), then ROLLS BACK. Nothing
is committed. Mirrors scripts/dryrun_024_025.py. 016c is COMMENT-only — additive and
non-destructive — so this is a clean-apply + coverage proof, not an RLS test.
"""
import os, re, psycopg2

with open(".env") as f:
    for line in f:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1); os.environ.setdefault(k, v.strip())


def body(path):
    sql = open(path).read()
    sql = re.sub(r"(?im)^\s*BEGIN\s*;\s*$", "", sql)
    sql = re.sub(r"(?im)^\s*COMMIT\s*;\s*$", "", sql)
    return sql


conn = psycopg2.connect(os.environ["DATABASE_URL"])
cur = conn.cursor()
ok = True


def check(label, cond):
    global ok
    ok = ok and cond
    print(f"   [{'PASS' if cond else 'FAIL'}] {label}")


try:
    cur.execute("BEGIN")
    cur.execute(body("migrations/016c_schema_comments_completion.sql"))
    print("== migration 016c applied in-tx ==")

    # every public base table now carries a table comment
    cur.execute("""
        SELECT count(*) FILTER (WHERE obj_description(c.oid) IS NOT NULL),
               count(*)
        FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' AND c.relkind='r'
    """)
    commented, total = cur.fetchone()
    check(f"every base table has a table comment ({commented}/{total})", commented == total)

    # no base table left fully uncommented (table comment OR any column comment)
    cur.execute("""
        SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' AND c.relkind='r'
          AND obj_description(c.oid) IS NULL
          AND NOT EXISTS (SELECT 1 FROM pg_attribute a
                          WHERE a.attrelid=c.oid AND a.attnum>0 AND NOT a.attisdropped
                            AND col_description(c.oid,a.attnum) IS NOT NULL)
    """)
    check("zero undocumented base tables remain", cur.fetchone()[0] == 0)

    # profiles is 12/12 (is_maintainer comment added)
    cur.execute("""
        SELECT count(*) FILTER (WHERE col_description(c.oid,a.attnum) IS NOT NULL), count(*)
        FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid
        JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' AND c.relname='profiles' AND a.attnum>0 AND NOT a.attisdropped
    """)
    pc, ptot = cur.fetchone()
    check(f"profiles fully commented ({pc}/{ptot})", pc == ptot)

    # the generated-col fix landed (taken_on warns NEVER insert + 428C9)
    cur.execute("""SELECT col_description(('public.supplement_intake_logs')::regclass,
                     (SELECT attnum FROM pg_attribute
                      WHERE attrelid='public.supplement_intake_logs'::regclass
                        AND attname='taken_on'))""")
    tk = cur.fetchone()[0] or ""
    check("taken_on comment says GENERATED + NEVER insert + 428C9",
          "GENERATED" in tk and "NEVER" in tk and "428C9" in tk)

    # dormant fossils flagged DORMANT
    for t in ("users_extended", "body_metrics_history"):
        cur.execute("SELECT obj_description(('public.'||%s)::regclass)", (t,))
        c = cur.fetchone()[0] or ""
        check(f"{t} marked DORMANT", c.startswith("DORMANT"))

    print(f"\n== DRY-RUN {'GREEN' if ok else 'RED'} ==")
finally:
    cur.execute("ROLLBACK")
    conn.close()
    print("== rolled back (nothing committed) ==")
