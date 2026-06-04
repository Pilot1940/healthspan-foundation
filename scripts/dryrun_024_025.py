"""Dry-run harness for migrations 024 (drop backup table) + 025 (query_audit reshape).
Applies both in ONE transaction, proves the end state under real RLS roles, then ROLLS
BACK. Nothing is committed. Mirrors scripts/dryrun_022_maintainer.py.
"""
import os, re, json, uuid, psycopg2

PC_AUTH  = "0b0e4093-6758-46f7-a6e2-311ef6828a86"
DEA_AUTH = "47501376-6adb-458e-8679-57a4a4176692"
PC_PROFILE = "21f69003-46f8-4e1c-a928-b1f694ce4aff"

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
    for m in ("024_drop_food_logs_dedup_backup.sql", "025_query_audit_catalog_kind.sql"):
        cur.execute(body(f"migrations/{m}"))
    print("== migrations 024 + 025 applied in-tx ==")

    # 024: backup table gone
    cur.execute("SELECT to_regclass('public.food_logs_dedup_backup_20260602')")
    check("food_logs_dedup_backup_20260602 dropped", cur.fetchone()[0] is None)

    # 025: column set is exactly the canonical shape
    cur.execute("""SELECT column_name FROM information_schema.columns
                   WHERE table_schema='public' AND table_name='query_audit'
                   ORDER BY column_name""")
    cols = {r[0] for r in cur.fetchall()}
    expected = {"id", "profile_id", "ts", "kind", "name_or_sql", "params",
                "row_count", "quality_verdict", "flagged"}
    check(f"query_audit columns == canonical set ({sorted(cols)})", cols == expected)
    check("dropped columns gone (intent/sql/plan)", not ({"intent", "sql", "plan"} & cols))

    # kind CHECK constraint present
    cur.execute("""SELECT 1 FROM pg_constraint WHERE conname='query_audit_kind_chk'""")
    check("query_audit_kind_chk present", cur.fetchone() is not None)

    # RLS still maintainer-only SELECT + owner INSERT (unchanged by 025)
    cur.execute("""SELECT policyname, cmd FROM pg_policies
                   WHERE schemaname='public' AND tablename='query_audit' ORDER BY policyname""")
    pols = dict(cur.fetchall())
    check("query_audit_sel + query_audit_ins policies intact",
          pols.get("query_audit_sel") == "SELECT" and pols.get("query_audit_ins") == "INSERT")

    def set_role(auth):
        cur.execute("SET LOCAL ROLE authenticated")
        cur.execute("SELECT set_config('request.jwt.claims', %s, true)",
                    ('{"sub":"%s","role":"authenticated"}' % auth,))

    # PC (maintainer/owner): both a catalog row and an ad-hoc row INSERT + read back
    print("== PC (maintainer) writes + reads the new shape ==")
    set_role(PC_AUTH)
    cur.execute("""INSERT INTO query_audit (profile_id, kind, name_or_sql, params, row_count, flagged)
                   VALUES (%s,'catalog','v_recovery_30d',%s::jsonb,12,false)""",
                (PC_PROFILE, json.dumps({"days": 30})))
    cur.execute("""INSERT INTO query_audit (profile_id, kind, name_or_sql, params, quality_verdict, row_count, flagged)
                   VALUES (%s,'adhoc',%s,%s::jsonb,%s::jsonb,0,true)""",
                (PC_PROFILE, "SELECT avg(recovery_score_pct) FROM whoop_cycles",
                 json.dumps({"intent": "recovery avg"}),
                 json.dumps({"ok": False, "notes": "aggregate over nullable"})))
    cur.execute("SELECT count(*) FILTER (WHERE kind='catalog'), count(*) FILTER (WHERE kind='adhoc') FROM query_audit")
    cat, adh = cur.fetchone()
    check("PC sees 1 catalog + 1 adhoc row", cat == 1 and adh == 1)

    # kind CHECK rejects a bad value
    try:
        cur.execute("SAVEPOINT s1")
        cur.execute("""INSERT INTO query_audit (profile_id, kind, name_or_sql) VALUES (%s,'bogus','x')""",
                    (PC_PROFILE,))
        check("kind CHECK rejects 'bogus'", False)
        cur.execute("RELEASE SAVEPOINT s1")
    except Exception:
        cur.execute("ROLLBACK TO SAVEPOINT s1")
        check("kind CHECK rejects 'bogus'", True)
    cur.execute("RESET ROLE")

    # Dea (non-maintainer): SELECT must still be 0 (maintainer-only RLS unchanged)
    print("== Dea (non-maintainer) ==")
    set_role(DEA_AUTH)
    cur.execute("SELECT count(*) FROM query_audit")
    check("Dea SELECT query_audit = 0 (maintainer-only intact)", cur.fetchone()[0] == 0)
    cur.execute("RESET ROLE")

    print(f"\n== DRY-RUN {'GREEN' if ok else 'RED'} ==")
finally:
    cur.execute("ROLLBACK")
    conn.close()
    print("== rolled back (nothing committed) ==")
