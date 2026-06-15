#!/usr/bin/env python3
"""self_test.py — HealthSpan read-only self-test + capabilities readout.

A first-class, NEVER-WRITES diagnostic. It prints WHERE IT IS RUNNING first (so a local
Cowork/Code pass can never be mistaken for a Claude-App egress pass), then an 8-step readout:
version, identity, connection + RLS-scope proof, context targets, maintainer check, freshness,
capabilities, verdict.

Read-only guarantees:
  * Only SELECTs — NEVER the catalog/ad-hoc helpers (lib.views.run_view /
    lib.sql_guard.run_adhoc_audited), which would log a row to query_audit.
  * The psycopg2 connection is rolled back and closed; the supabase client only selects.

Usage:  python scripts/self_test.py [config_path]
        (default: config/pc_skill.config.json, else the first config/*.config.json)
"""
from __future__ import annotations

import os
import re
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from lib import context as ctxm
from lib import db as dbm

DIV = "─" * 70


# ---------------------------------------------------------------------------
# config discovery (records WHERE the config came from — a runtime signal)
# ---------------------------------------------------------------------------

def _discover_config() -> tuple[str | None, str]:
    """Return (config_path, source_label). source_label is a runtime signal."""
    if len(sys.argv) > 1:
        return sys.argv[1], "explicit arg"
    env = os.environ.get("HEALTHSPAN_SKILL_CONFIG")
    if env:
        return env, "$HEALTHSPAN_SKILL_CONFIG"
    cfg_dir = os.path.join(ROOT, "config")
    preferred = os.path.join(cfg_dir, "pc_skill.config.json")
    if os.path.isfile(preferred):
        return preferred, "repo config/ (filesystem)"
    if os.path.isdir(cfg_dir):
        for fn in sorted(os.listdir(cfg_dir)):
            if fn.endswith(".config.json") and "example" not in fn:
                return os.path.join(cfg_dir, fn), "repo config/ (filesystem)"
    return None, "NONE on filesystem"


def _git_uptree() -> str | None:
    """Nearest ancestor dir containing a .git (→ a working repo = Cowork/Code)."""
    d = ROOT
    while True:
        if os.path.isdir(os.path.join(d, ".git")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


# ---------------------------------------------------------------------------
# mode-agnostic read helpers (SELECT-only)
# ---------------------------------------------------------------------------

def _pg(conn, sql):
    cur = conn.cursor()
    cur.execute(sql)
    return cur.fetchone()


def _probe_psycopg2(conn) -> dict:
    out = {}
    out["current_user"] = _pg(conn, "SELECT current_user")[0]
    out["whoop_cycles"] = _pg(conn, "SELECT count(*) FROM whoop_cycles")[0]
    out["distinct_profiles"] = _pg(conn, "SELECT count(DISTINCT profile_id) FROM whoop_cycles")[0]
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT profile_id FROM whoop_cycles")
    out["pids"] = [str(r[0]) for r in cur.fetchall()]
    out["is_maintainer"] = bool(_pg(conn, "SELECT public.is_maintainer()")[0])
    out["latest_cycle"] = _pg(conn, "SELECT max(cycle_start)::date FROM whoop_cycles")[0]
    out["latest_sleep"] = _pg(conn, "SELECT max(cycle_start)::date FROM whoop_sleeps")[0]
    out["latest_workout"] = _pg(conn, "SELECT max(workout_start)::date FROM whoop_workouts")[0]
    out["food_today"] = _pg(conn, "SELECT count(*) FROM food_logs WHERE log_date = current_date")[0]
    return out


def _probe_supabase(client) -> dict:
    """Best-effort equivalents over PostgREST (App-compatible path)."""
    out = {}
    def count(tbl):
        return client.table(tbl).select("id", count="exact", head=True).execute().count
    out["whoop_cycles"] = count("whoop_cycles")
    rows = client.table("whoop_cycles").select("profile_id").limit(1000).execute().data
    pids = sorted({r["profile_id"] for r in rows})
    out["pids"] = pids
    out["distinct_profiles"] = len(pids)
    try:
        out["is_maintainer"] = bool(client.rpc("is_maintainer").execute().data)
    except Exception:
        out["is_maintainer"] = "n/a (supabase_client — needs is_maintainer RPC)"
    def latest(tbl, col):
        d = client.table(tbl).select(col).order(col, desc=True).limit(1).execute().data
        return (d[0][col][:10] if d else None)
    out["latest_cycle"] = latest("whoop_cycles", "cycle_start")
    out["latest_sleep"] = latest("whoop_sleeps", "cycle_start")
    out["latest_workout"] = latest("whoop_workouts", "workout_start")
    out["food_today"] = "n/a (supabase_client)"
    return out


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    print(DIV)
    print("HealthSpan — SELF-TEST  (read-only · never writes)")
    print(DIV)

    cfg_path, cfg_source = _discover_config()
    cfg = dbm.load_config(cfg_path) if cfg_path else None
    mode = (cfg or {}).get("connection", {}).get("mode")
    privileged = bool((cfg or {}).get("connection", {}).get("direct_role", {}).get("privileged"))

    # ===================== STEP 0 — WHERE AM I RUNNING =====================
    print("\n[0] WHERE AM I RUNNING")
    mode_meaning = {
        "direct_role": "psycopg2/local — Cowork or Claude Code run. This does NOT test "
                       "Claude-App egress.",
        "supabase_client": "HTTPS/PostgREST — App-compatible path. If this connects, App "
                           "egress is PROVEN.",
    }.get(mode, "unknown mode")
    if privileged:
        mode_meaning = ("psycopg2/local UNRESTRICTED — keeps the postgres role: RLS BYPASSED, "
                        "DELETE/DDL on every profile. The maintainer's own admin bundle.")
    print(f"    connection mode : {mode}{' (privileged)' if privileged else ''}  →  {mode_meaning}")
    git = _git_uptree()
    fs_context = os.path.isdir(os.path.join(ROOT, "context"))
    print(f"    config source   : {cfg_source}" + (f"  ({cfg_path})" if cfg_path else ""))
    print(f"    .git up-tree    : {git or 'none'}  ({'repo present' if git else 'no repo'})")
    print(f"    cwd             : {os.getcwd()}")
    print(f"    context source  : {'filesystem context/' if fs_context else 'NOT on filesystem (Project knowledge / pasted)'}")

    # conclude runtime from the signals (don't over-claim)
    if mode == "supabase_client":
        runtime, rt_tag = "Claude App / claude.ai (bundled)", "App · supabase_client"
    elif mode == "direct_role" and git and "filesystem" in cfg_source:
        runtime, rt_tag = "Cowork/Code (repo)", "Cowork · direct_role"
    elif mode == "direct_role":
        runtime, rt_tag = "local direct_role (bundled/no-repo)", "local · direct_role"
    else:
        runtime, rt_tag = (f"unknown — signals: mode={mode}, git={'y' if git else 'n'}, "
                           f"cfg={cfg_source}"), "unknown"
    print(f"    RUNTIME         : {runtime}")

    if cfg is None:
        print("\nVERDICT: BLOCKED — no config on filesystem. On claude.ai/App, load "
              "<who>.config.json + <who>.context.md from Project knowledge (or paste once). "
              f"[{rt_tag}]")
        return 2

    # ===================== STEP 1 — VERSION =====================
    print("\n[1] VERSION")
    ver = "(unknown)"
    try:
        with open(os.path.join(ROOT, "CHANGELOG.md")) as f:
            for line in f:
                if line.startswith("## "):
                    ver = line[3:].strip()
                    break
    except Exception:
        pass
    print(f"    skill=healthspan  changelog_head={ver}")
    migs = sorted(fn for fn in os.listdir(os.path.join(ROOT, "migrations")) if fn.endswith(".sql"))
    print(f"    migrations on disk: {len(migs)} (HEAD {migs[-1] if migs else 'none'})")
    # schema-map freshness — WARN if the generated_at stamp is >30 days old
    smap = os.path.join(ROOT, "docs", "SCHEMA-MAP.md")
    try:
        head = open(smap).read(2000)
        m = re.search(r"generated_at:\s*([0-9T:\-]+Z)", head)
        if not m:
            print("    schema-map: ⚠ WARN no generated_at stamp — regenerate scripts/gen_schema_map.py")
        else:
            gen = datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - gen).days
            flag = "⚠ WARN >30d STALE — regenerate" if age > 30 else "ok"
            print(f"    schema-map: generated_at={m.group(1)} (age {age}d) {flag}")
    except FileNotFoundError:
        print("    schema-map: ⚠ WARN docs/SCHEMA-MAP.md missing")

    # ===================== STEP 2 — IDENTITY =====================
    print("\n[2] IDENTITY  (from BAKED config — not asked)")
    print(f"    profile_id={cfg['profile_id']}  display_name={cfg['display_name']}  "
          f"is_owner={cfg.get('is_owner')}  is_minor={cfg.get('is_minor')}  mode={mode}")

    # ===================== STEP 3 — CONNECTION + RLS =====================
    print("\n[3] CREDENTIALS / CONNECTION + RLS SCOPE")
    probe = None
    handle = None
    driver = None
    try:
        handle, driver = dbm.get_app_connection(cfg)
        probe = _probe_psycopg2(handle) if driver == "psycopg2" else _probe_supabase(handle)
        print(f"    connected=True  driver={driver}  whoop_cycles={probe['whoop_cycles']}")
        if privileged:
            # Unrestricted: the CORRECT state is role=postgres + maintainer + sees ALL profiles.
            cu = probe.get("current_user")
            scoped_ok = (cu == "postgres" and probe["is_maintainer"] is True)
            print(f"    UNRESTRICTED: current_user={cu}  (expect 'postgres', RLS bypassed)")
            print(f"    cross-profile: distinct_profiles={probe['distinct_profiles']}  "
                  f"pids={probe['pids']}  (sees ALL profiles — expected)")
            print(f"    maintainer: is_maintainer()={probe['is_maintainer']}  "
                  f"→ privileged-mode OK? {scoped_ok}")
        elif probe["is_maintainer"] is True:
            # A maintainer legitimately sees self + managed family via has_profile_access —
            # >1 profile is EXPECTED; "scoped to exactly self" is only the non-maintainer rule.
            scoped_ok = cfg["profile_id"] in probe["pids"]
            print(f"    RLS scope (maintainer): distinct_profiles={probe['distinct_profiles']}  "
                  f"pids={probe['pids']}  self_visible? {scoped_ok}  (family-wide is expected)")
            print(f"    maintainer: is_maintainer()={probe['is_maintainer']}")
        else:
            scoped_ok = (probe["distinct_profiles"] == 1 and probe["pids"] == [cfg["profile_id"]])
            print(f"    RLS scope: distinct_profiles={probe['distinct_profiles']}  pids={probe['pids']}  "
                  f"== my_profile? {scoped_ok}")
            print(f"    maintainer: is_maintainer()={probe['is_maintainer']}")
        if driver == "psycopg2":
            handle.rollback()        # reset the probe txn; keep OPEN for the DB-first context read below
    except Exception as e:
        print(f"    connected=False  ERROR={str(e).splitlines()[0]!r}")
        print("    (egress/credential issue — the real unknown for App runs)")

    # ===================== STEP 4 — CONTEXT =====================
    print("\n[4] CONTEXT  (targets from context — DB-first (mig 073), file fallback; vs config, not defaults)")
    try:
        # Pass the live handle so get_context reads profiles.context_md (mig 073). _source
        # reports which tier served it: 'db' (live), 'file' (bundle fallback), or 'project_knowledge'.
        ctx = ctxm.get_context(cfg, conn=handle)
        g = lambda k: ctxm.get_target(ctx, k)
        print(f"    is_minor={ctx.get('is_minor')}  source={ctx.get('_source')}  "
              f"path={ctx.get('_path') or '(DB)'}")
        for k in ("daily_calories", "maintenance_kcal", "calorie_floor", "protein_g",
                  "vo2max_target_jan2027", "vo2max_target_longterm", "hrv_target_ms"):
            print(f"      {k:24s}= {g(k)}")
    except Exception as e:
        print(f"    CONTEXT ERROR: {str(e).splitlines()[0]!r}")
    finally:
        if driver == "psycopg2" and handle is not None:
            try:
                handle.rollback(); handle.close()
            except Exception:
                pass

    # ===================== STEP 5 — MAINTAINER =====================
    print("\n[5] MAINTAINER CHECK")
    m = probe.get("is_maintainer") if probe else "(no connection)"
    print(f"    is_maintainer={m}  → ingest_health + query_log surfaces "
          f"{'AVAILABLE to me' if m is True else 'hidden / unknown'} "
          f"(maintainer-only RLS; a non-maintainer sees 0 rows)")

    # ===================== STEP 6 — FRESHNESS =====================
    print("\n[6] FRESHNESS")
    if probe:
        print(f"    latest_cycle={probe['latest_cycle']}  latest_sleep={probe['latest_sleep']}  "
              f"latest_workout={probe['latest_workout']}  food_logs_today={probe['food_today']}")
    else:
        print("    (no connection — freshness unavailable)")

    # ===================== STEP 7 — CAPABILITIES =====================
    print("\n[7] CAPABILITIES")
    print("    INGEST  : food (photo+manual→LLM→staging), lab/DEXA (OCR→biomarkers),")
    print("              WHOOP (API sync refresh_recent + screenshot), supplement (incl. photo)")
    print("    ANALYSE : recovery/HRV/sleep trends (NULL-aware), workout zones, interval/HR-drop")
    print("              coaching, VO2 status (measured≠estimated), abnormal labs, India-vs-travel")
    print("    PLAN    : training plans, multiple concurrent goals (direction-aware)")
    print("    REPORT  : markdown / dashboard / xlsx-PDF — all off the lib/views catalog")
    print("    GUARDS  : RLS (proven), read-only role, schema-map+TRAPs, SQL guard (EXPLAIN),")
    print("              cite guard, implausibility gate, query-audit logging")

    # ===================== STEP 8 — VERDICT =====================
    print("\n[8] VERDICT")
    if privileged:
        ready = bool(probe and probe.get("current_user") == "postgres"
                     and probe["is_maintainer"] is True)
        if ready:
            print(f"    READY — UNRESTRICTED (postgres role, maintainer, RLS bypassed) [{rt_tag}]")
            print(DIV)
            return 0
        blocker = ("no connection" if not probe
                   else f"privileged check failed (current_user={probe.get('current_user')}, "
                        f"is_maintainer={probe['is_maintainer']})")
        print(f"    BLOCKED — {blocker}  [{rt_tag}]")
        print(DIV)
        return 1
    if probe and probe["is_maintainer"] is True:
        # maintainer: READY when connected and own profile is visible (family-wide expected)
        ready = cfg["profile_id"] in probe["pids"]
        tag = "READY (maintainer — family-wide scope)"
    else:
        ready = bool(probe and probe["distinct_profiles"] == 1
                     and probe["pids"] == [cfg["profile_id"]])
        tag = "READY"
    if ready:
        print(f"    {tag} ({rt_tag})")
        print(DIV)
        return 0
    blocker = ("no Supabase egress / connection failed" if not probe
               else "own profile not visible" if probe["is_maintainer"] is True
               else "RLS scope not 1==my_profile")
    print(f"    BLOCKED — {blocker}  [{rt_tag}]")
    print(DIV)
    return 1


if __name__ == "__main__":
    sys.exit(main())
