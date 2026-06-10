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

# Tables documented as maintainer-only machinery (migrations 022/058): every SELECT
# policy on these must gate SOLELY on is_maintainer() — never has_profile_access
# (that was exactly the mig-022 miss on three stg_* tables, fixed by 058).
MAINTAINER_ONLY = ['query_audit', 'wearable_sync_log', 'wearable_sync_errors',
                   'push_log', 'telegram_processed_updates',
                   'stg_biomarker_review', 'stg_food_log_review', 'stg_food_rule_review',
                   'stg_supplement_intake_review', 'stg_test_result_review']

def cmd_verify(_):
    c = connect(); cur = c.cursor()
    # SUBJECT tables are derived at runtime (the old hardcoded list had gone stale at
    # 32 of 61 tables): every base table carrying a profile_id column holds per-person
    # health data and must be RLS-covered. Catalog/shared tables (no profile_id) are
    # reported but not failed — read-all policies are legitimate there.
    cur.execute("""SELECT c.relname, c.relrowsecurity FROM pg_class c
                   JOIN pg_namespace n ON n.oid = c.relnamespace
                   WHERE n.nspname='public' AND c.relkind='r' ORDER BY 1""")
    rls = dict(cur.fetchall())
    cur.execute("""SELECT table_name FROM information_schema.columns
                   WHERE table_schema='public' AND column_name='profile_id'""")
    subject = sorted(set(t for (t,) in cur.fetchall()) & set(rls))
    print(f"== {len(rls)} base tables; {len(subject)} profile-scoped (runtime-derived) ==")
    no_rls = sorted(t for t, enabled in rls.items() if not enabled)
    print("  RLS disabled (must be none):", no_rls or "NONE OK")

    print("== RLS leaks for non-service roles (must be none) ==")
    # Leak = open WRITE anywhere (with_check=true — the mig-061 class) OR open READ on a
    # profile-scoped table. Read-all SELECT on a catalog table (supplements,
    # metric_definitions, system_config…) is by design and only counted.
    cur.execute("""SELECT tablename, policyname, cmd, roles, qual, with_check FROM pg_policies
       WHERE schemaname='public' AND (qual='true' OR with_check='true')""")
    leaks, catalog_reads = [], 0
    for tn, pol, cmd, roles, qual, wc in cur.fetchall():
        if 'service_role' in (roles or ''):
            continue
        if wc == 'true' or (qual == 'true' and tn in subject):
            leaks.append((tn, pol, cmd, roles))
        else:
            catalog_reads += 1
    print("  LEAKS:", leaks or f"NONE OK ({catalog_reads} catalog read-all policies, by design)")

    print("== RLS coverage on profile-scoped tables ==")
    # Covered = a policy scoping by has_profile_access / is_maintainer / auth.uid()
    # (family_memberships + ingestion_tokens scope by uid directly — has_profile_access
    # would be circular there), OR zero policies (RLS on + no policy = deny-all = locked).
    cur.execute("SELECT tablename, qual, with_check FROM pg_policies WHERE schemaname='public'")
    has_policy, covered, maint = set(), set(), set()
    for tn, qual, wc in cur.fetchall():
        has_policy.add(tn)
        blob = f"{qual or ''} {wc or ''}"
        if ("has_profile_access" in blob or "is_maintainer" in blob
                or "auth.uid()" in blob):
            covered.add(tn)
        if "is_maintainer" in blob:
            maint.add(tn)
    miss = [t for t in subject if t not in covered and t in has_policy]
    locked = [t for t in subject if t not in has_policy]
    print("  missing:", miss or f"NONE OK ({len(covered & set(subject))}/{len(subject)} scoped"
                                f"{', ' + str(len(locked)) + ' deny-all' if locked else ''})")
    if locked:
        print("  deny-all (no policies — service_role writers only):", locked)

    print("== maintainer-only tables: SELECT must gate on is_maintainer alone ==")
    # cmd IN (SELECT, ALL): an ALL policy grants SELECT too — the mig-022 miss was
    # exactly an over-broad ALL/has_profile_access policy on three stg_* tables.
    cur.execute("""SELECT tablename, policyname, qual FROM pg_policies
       WHERE schemaname='public' AND tablename=ANY(%s) AND cmd IN ('SELECT','ALL')""",
                (MAINTAINER_ONLY,))
    bad, seen = [], set()
    for tn, pol, qual in cur.fetchall():
        seen.add(tn)
        q = qual or ''
        if 'is_maintainer' not in q or 'has_profile_access' in q:
            bad.append((tn, pol, qual))
    no_sel = [t for t in MAINTAINER_ONLY if t in rls and t not in seen]
    print("  mis-gated (must be none):", bad or "NONE OK")
    if no_sel:
        print("  no SELECT policy at all (deny-all — fine):", no_sel)
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

def cmd_mint_link_code(args):
    """Mint a single-use Telegram link code for a profile.

    Usage: mint-link-code <profile_id>
    The code is printed once — send it to the person who will DM the bot.
    They send the code to the bot; the Edge fn activates their identity.
    """
    if not args:
        sys.exit("usage: mint-link-code <profile_id>")
    profile_id = args[0]
    import secrets
    code = secrets.token_urlsafe(16)
    c = connect(readonly=False); cur = c.cursor()
    cur.execute("""
        INSERT INTO public.telegram_link_codes (code, profile_id)
        VALUES (%s, %s)
        RETURNING expires_at
    """, (code, profile_id))
    expires_at = cur.fetchone()[0]
    c.commit()
    print("TELEGRAM LINK CODE (send this to the person — single-use, expires " + str(expires_at) + "):")
    print("  " + code)
    print("Send this code as a DM to @chitalkar_healthspan_bot to link your account.")

def cmd_setup_telegram_storage(_):
    """Create the health-media private Storage bucket if it does not exist.

    Usage: setup-telegram-storage
    Idempotent — safe to re-run.
    """
    import json
    from urllib.request import Request, urlopen
    from urllib.error import HTTPError
    url = os.environ["SUPABASE_URL"].rstrip("/")
    key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    headers = {"Authorization": f"Bearer {key}", "apikey": key, "Content-Type": "application/json"}

    # Check existence
    try:
        req = Request(f"{url}/storage/v1/bucket/health-media", headers=headers)
        resp = urlopen(req)
        data = json.loads(resp.read())
        print(f"Bucket health-media already exists (public={data.get('public', '?')}). OK.")
        return
    except HTTPError as e:
        if e.code != 400:
            raise

    # Create
    body = json.dumps({"id": "health-media", "name": "health-media", "public": False}).encode()
    req = Request(f"{url}/storage/v1/bucket", data=body, headers=headers, method="POST")
    resp = urlopen(req)
    print("Created bucket health-media (private). OK.")
    print(resp.read().decode())

CMDS = {"whoami": cmd_whoami, "profiles": cmd_profiles, "backup": cmd_backup,
        "apply": cmd_apply, "verify": cmd_verify, "mint-token": cmd_mint_token,
        "mint-link-code": cmd_mint_link_code,
        "setup-telegram-storage": cmd_setup_telegram_storage}

if __name__ == "__main__":
    _load_env()
    if len(sys.argv) < 2 or sys.argv[1] not in CMDS:
        print(__doc__); sys.exit(0 if len(sys.argv) < 2 else 1)
    CMDS[sys.argv[1]](sys.argv[2:])
