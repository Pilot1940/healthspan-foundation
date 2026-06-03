"""Dry-run harness for migrations 020/021/022 (V3-2) — applies all three in ONE
transaction, proves the maintainer model end-to-end under real RLS roles, then
ROLLS BACK. Nothing is committed. The proof the spec's "0 rows" check can't give:
the full Dea WRITE chain still succeeds after SELECT goes maintainer-only.
"""
import os, re, psycopg2

PC_AUTH  = "0b0e4093-6758-46f7-a6e2-311ef6828a86"
DEA_AUTH = "47501376-6adb-458e-8679-57a4a4176692"
DEA_PROFILE = "3eed5503-a26f-4b88-bb76-075208fa5de3"
TABLES = ("query_audit", "wearable_sync_log", "wearable_sync_errors",
          "stg_biomarker_review", "stg_food_log_review")

with open(".env") as f:
    for line in f:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1); os.environ.setdefault(k, v.strip())

def body(path):
    """Migration SQL minus its outer BEGIN/COMMIT (we manage one big tx)."""
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
    for m in ("020_multiple_concurrent_goals.sql", "021_query_audit.sql", "022_maintainer_model.sql"):
        cur.execute(body(f"migrations/{m}"))
    print("== migrations 020/021/022 applied in-tx ==")

    # helper truth table (run as definer; service role)
    cur.execute("SELECT public.is_maintainer(%s), public.is_maintainer(%s)", (PC_AUTH, DEA_AUTH))
    pc_m, dea_m = cur.fetchone()
    check("is_maintainer(PC)=true", pc_m is True)
    check("is_maintainer(Dea)=false", dea_m is False)

    def set_role(auth):
        cur.execute("SET LOCAL ROLE authenticated")
        cur.execute("SELECT set_config('request.jwt.claims', %s, true)",
                    ('{"sub":"%s","role":"authenticated"}' % auth,))

    # ---- DEA: SELECT must be 0 on all four; WRITE chain must still succeed --------
    print("== Dea (non-maintainer) ==")
    set_role(DEA_AUTH)
    for t in TABLES:
        cur.execute(f"SELECT count(*) FROM {t}")
        check(f"Dea SELECT {t} = 0", cur.fetchone()[0] == 0)

    # full write chain (mirrors contract.open_sync_log → stage → log_error → close)
    import uuid
    sid = str(uuid.uuid4())
    try:
        cur.execute("""INSERT INTO wearable_sync_log
              (id, provider, method, sync_type, status, profile_id,
               records_in, records_upserted, records_skipped, records_failed, started_at)
              VALUES (%s,'manual','manual','ingest','in_progress',%s,0,0,0,0,now())""",
              (sid, DEA_PROFILE))
        check("Dea INSERT wearable_sync_log (no RETURNING)", True)
    except Exception as e:
        check(f"Dea INSERT wearable_sync_log — {str(e).splitlines()[0]}", False)
    try:
        cur.execute("""INSERT INTO stg_food_log_review (id, profile_id, description, calories,
                       confidence, status) VALUES (%s,%s,'probe meal',20,0.9,'pending')""",
                    (str(uuid.uuid4()), DEA_PROFILE))
        check("Dea INSERT stg_food_log_review (stage)", True)
    except Exception as e:
        check(f"Dea stage — {str(e).splitlines()[0]}", False)
    try:
        cur.execute("""INSERT INTO wearable_sync_errors (id, sync_log_id, record_ref,
                       error_code, error_message) VALUES (%s,%s,'rec','implausible','probe')""",
                    (str(uuid.uuid4()), sid))
        check("Dea INSERT wearable_sync_errors (cross-table cascade fixed)", True)
    except Exception as e:
        check(f"Dea log_error — {str(e).splitlines()[0]}", False)
    try:
        # close via the SECURITY DEFINER finaliser (a plain owner UPDATE…WHERE can't
        # locate the row under the maintainer-only SELECT policy)
        cur.execute("SELECT public.hs_close_sync_log(%s,'success',0,0,0,0)", (sid,))
        cur.execute("SELECT public.is_maintainer()")  # sanity: still Dea (non-maintainer)
        # confirm the close actually took effect (read back as maintainer later)
        check("Dea close_sync_log via hs_close_sync_log (no error)", True)
    except Exception as e:
        check(f"Dea close_sync_log — {str(e).splitlines()[0]}", False)
    # she still can't SELECT what she wrote
    cur.execute("SELECT count(*) FROM wearable_sync_log")
    check("Dea STILL sees 0 wearable_sync_log after her own writes", cur.fetchone()[0] == 0)
    cur.execute("RESET ROLE")

    # ---- PC (maintainer): SELECT sees rows on all four --------------------------
    print("== PC (maintainer) ==")
    set_role(PC_AUTH)
    for t in ("wearable_sync_log", "wearable_sync_errors", "stg_food_log_review"):
        cur.execute(f"SELECT count(*) FROM {t}")
        n = cur.fetchone()[0]
        check(f"PC SELECT {t} sees rows (n={n}, incl. Dea's in-tx writes)", n >= 1)
    # the definer close actually finalised Dea's run (status flipped to 'success')
    cur.execute("SELECT status, completed_at IS NOT NULL FROM wearable_sync_log WHERE id=%s", (sid,))
    st = cur.fetchone()
    check(f"Dea's run finalised by hs_close_sync_log (status={st[0]}, completed={st[1]})",
          st is not None and st[0] == "success" and st[1] is True)
    cur.execute("RESET ROLE")

    print(f"\n== DRY-RUN {'GREEN' if ok else 'RED'} ==")
finally:
    cur.execute("ROLLBACK")
    conn.close()
    print("== rolled back (nothing committed) ==")
