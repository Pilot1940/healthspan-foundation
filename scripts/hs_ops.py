#!/usr/bin/env python3
"""
hs_ops.py — HealthSpan basic operating scripts (one CLI for day-to-day DB ops).

Reads DATABASE_URL from ../.env (Supabase). The DB password contains '#'/'!'/'^',
so we parse the URL by hand (urllib.parse.urlparse mis-handles '#' as a fragment).

pg_dump/psql must be v17 to match the PG 17.6 server — we resolve the Homebrew
postgresql@17 binaries explicitly.

Commands:
  python hs_ops.py whoami                 # show connection target + profiles
  python hs_ops.py backup [label]         # full public-schema backup (.sql + .dump)
  python hs_ops.py apply <migration.sql>  # apply a migration (ON_ERROR_STOP, atomic)
  python hs_ops.py verify                 # RLS isolation + structural health checks
  python hs_ops.py mint-token <profile_id> [label] [methods]  # ingestion token (shown ONCE)
  python hs_ops.py profiles               # list profiles + ids (to feed mint-token)

Connection modes: psycopg2 uses the pooler (6543, transaction mode). pg_dump/psql
use SESSION mode (5432) — required for dump and for SET LOCAL ROLE tests.
"""
import os, sys, subprocess, datetime
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PG17 = "/opt/homebrew/opt/postgresql@17/bin"   # pg_dump/psql 17.x

def _load_env():
    envp = os.path.join(ROOT, ".env")
    with open(envp) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k, v.strip())
    if "DATABASE_URL" not in os.environ:
        sys.exit("DATABASE_URL not found in .env")

def parts():
    """Robustly split postgresql://user:pass@host:port/db (pass may contain '#')."""
    u = os.environ["DATABASE_URL"].split("://", 1)[1]
    creds, hostpart = u.rsplit("@", 1)
    user, pw = creds.split(":", 1)
    hostport, _, db = hostpart.partition("/")
    host, _, port = hostport.partition(":")
    return dict(host=host, port=port or "6543", user=user, pw=pw, db=db or "postgres")

def pg_env(session=True):
    p = parts()
    e = dict(os.environ)
    e.update(PGHOST=p["host"], PGPORT="5432" if session else p["port"],
             PGUSER=p["user"], PGPASSWORD=p["pw"], PGDATABASE=p["db"])
    return e

def connect(readonly=True):
    import psycopg2
    c = psycopg2.connect(os.environ["DATABASE_URL"])
    if readonly:
        c.set_session(readonly=True)
    return c

# ---------------------------------------------------------------- commands
def cmd_whoami(_):
    p = parts()
    print(f"host={p['host']}  db={p['db']}  user={p['user']}  (pooler {p['port']} / session 5432)")
    c = connect(); cur = c.cursor()
    cur.execute("select current_database(), version()")
    print(cur.fetchone()[1].split(',')[0])
    cmd_profiles(_)

def cmd_profiles(_):
    c = connect(); cur = c.cursor()
    cur.execute("""SELECT p.id, p.display_name, p.relationship,
                          p.auth_user_id IS NOT NULL AS has_login, f.name
                   FROM profiles p LEFT JOIN families f ON f.id=p.family_id
                   ORDER BY p.relationship='self' DESC, p.display_name""")
    print("\nPROFILES:")
    for pid, name, rel, login, fam in cur.fetchall():
        print(f"  {pid}  {name:24s} {rel:6s} login={login}  family={fam}")

def cmd_backup(args):
    label = args[0] if args else datetime.date.today().isoformat()
    bkdir = os.path.join(ROOT, "backups"); os.makedirs(bkdir, exist_ok=True)
    base = os.path.join(bkdir, f"healthspan-fullbackup-{label}")
    e = pg_env(session=True)
    print(f"Dumping public schema -> {base}.sql / .dump")
    subprocess.run([f"{PG17}/pg_dump", "--schema=public", "--no-owner",
                    "--no-privileges", "-f", base + ".sql"], env=e, check=True)
    subprocess.run([f"{PG17}/pg_dump", "--schema=public", "-Fc",
                    "-f", base + ".dump"], env=e, check=True)
    for ext in (".sql", ".dump"):
        sz = os.path.getsize(base + ext)
        print(f"  {base+ext}  ({sz:,} bytes)")
    print("Restore (custom archive):  pg_restore --clean --if-exists -d <session-url> "
          + base + ".dump")

def cmd_apply(args):
    if not args:
        sys.exit("usage: apply <migration.sql>")
    mig = args[0]
    e = pg_env(session=True)
    print(f"Applying {mig} (ON_ERROR_STOP, atomic BEGIN/COMMIT)...")
    r = subprocess.run([f"{PG17}/psql", "-v", "ON_ERROR_STOP=1", "-f", mig], env=e)
    sys.exit(r.returncode)

# whoop_journal is a VIEW since 007 (security_invoker, RLS via whoop_journal_entries)
SUBJECT = ['sprints','whoop_cycles','whoop_sleeps','whoop_workouts','whoop_journal_entries',
 'biomarkers','food_logs','daily_logs','daily_log_metrics','weight_logs',
 'body_metrics_history','healthspan_tests','user_goals','food_rules',
 'biomarker_targets','documents','trend_alerts','wearable_sync_log',
 'stg_biomarker_review','stg_food_rule_review','stg_test_result_review',
 'travel_log','supplement_regimens','supplement_intake_logs','hr_zone_config',
 'workout_intervals','workout_hr_samples']

def cmd_verify(_):
    c = connect(); cur = c.cursor()
    print("== RLS: surviving authenticated/anon qual=true policies (must be none) ==")
    cur.execute("""SELECT tablename, policyname, roles FROM pg_policies
       WHERE schemaname='public' AND tablename=ANY(%s) AND qual='true'""", (SUBJECT,))
    leaks = [r for r in cur.fetchall() if 'service_role' not in (r[2] or '')]
    print("  LEAKS:", leaks or "NONE OK")
    print("== RLS coverage (profile_access OR maintainer-only) ==")
    # A table is covered if any policy scopes by has_profile_access (per-profile) OR by
    # is_maintainer (the maintenance tables — migration 022, maintainer-only SELECT).
    cur.execute("SELECT tablename, qual, with_check FROM pg_policies WHERE schemaname='public'")
    covered, maint = set(), set()
    for tn, qual, wc in cur.fetchall():
        blob = f"{qual or ''} {wc or ''}"
        if "has_profile_access" in blob or "is_maintainer" in blob:
            covered.add(tn)
        if "is_maintainer" in blob:
            maint.add(tn)
    miss = [t for t in SUBJECT if t not in covered]
    print("  missing:", miss or f"NONE OK ({len(covered & set(SUBJECT))}/{len(SUBJECT)})")
    print("  maintainer-only tables:", sorted(maint) or "none")
    print("== live RLS read test under SET ROLE authenticated ==")
    cur.execute("SELECT id FROM profiles WHERE relationship='self' LIMIT 1")
    pc = cur.fetchone()
    c2 = connect(readonly=False); cu = c2.cursor()
    cur.execute("SELECT auth_user_id FROM profiles WHERE relationship='self' LIMIT 1")
    pc_auth = cur.fetchone()[0]
    for who, sub in [("owner PC", pc_auth), ("random user", "11111111-1111-1111-1111-111111111111")]:
        cu.execute("BEGIN")
        cu.execute("SET LOCAL ROLE authenticated")
        cu.execute("SELECT set_config('request.jwt.claims', %s, true)",
                   ('{"sub":"%s","role":"authenticated"}' % sub,))
        cu.execute("SELECT count(*) FROM whoop_cycles"); n = cu.fetchone()[0]
        cu.execute("ROLLBACK")
        print(f"  {who}: whoop_cycles visible = {n}")
    print("== data intact (profile_id backfilled) ==")
    for t in ['whoop_cycles','whoop_workouts','biomarkers','food_logs','sprints']:
        cur.execute(f"SELECT count(*), count(*) FILTER (WHERE profile_id IS NULL) FROM {t}")
        tot, nul = cur.fetchone(); print(f"  {t}: {tot} rows, {nul} null profile_id")

def cmd_mint_token(args):
    if not args:
        sys.exit("usage: mint-token <profile_id> [label] [comma,methods]")
    profile_id = args[0]
    label = args[1] if len(args) > 1 else None
    methods = args[2].split(",") if len(args) > 2 else ['photo', 'screenshot']
    c = connect(readonly=False); cur = c.cursor()
    cur.execute("SELECT mint_ingestion_token(%s, %s, %s)", (profile_id, label, methods))
    tok = cur.fetchone()[0]; c.commit()
    print("INGESTION TOKEN (shown once — embed in the .skill, store encrypted):")
    print("  " + tok)

CMDS = {"whoami": cmd_whoami, "profiles": cmd_profiles, "backup": cmd_backup,
        "apply": cmd_apply, "verify": cmd_verify, "mint-token": cmd_mint_token}

if __name__ == "__main__":
    _load_env()
    if len(sys.argv) < 2 or sys.argv[1] not in CMDS:
        print(__doc__); sys.exit(0 if len(sys.argv) < 2 else 1)
    CMDS[sys.argv[1]](sys.argv[2:])
