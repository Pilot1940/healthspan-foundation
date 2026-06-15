#!/usr/bin/env python3
"""backfill_073_context.py — seed profiles.context_md from the context/*.context.md files.

Companion to migration 073. The migration ships the SCHEMA (column + guard trigger +
maintainer_set_profile_context RPC); this seeds the personal BODIES separately (transportable
schema — same split as the mig 063 strength seed / mig 064 personal food-ref seed).

Each context/<slug>.context.md declares its own `profile_id:` in its header — we route every
file's body to the profile it names, so there is no hardcoded slug→uuid mapping. The write
goes through maintainer_set_profile_context(), which is maintainer-only; since we connect as
the privileged DB role (no auth.uid()), we set request.jwt.claims to a MAINTAINER membership
uid for the transaction (exactly how scripts/hs_ops.py verify simulates an authenticated role).

Idempotent: a profile whose context_md is already populated is SKIPPED unless --force.

Usage:
    python3 scripts/backfill_073_context.py            # seed only NULL context_md rows
    python3 scripts/backfill_073_context.py --force    # overwrite (re-seed from files)
    python3 scripts/backfill_073_context.py --dry-run   # report only, no writes

Reads DATABASE_URL from $DATABASE_URL or ../.env (same convention as the integration tests).
"""
from __future__ import annotations

import glob
import json
import os
import sys
from pathlib import Path

import psycopg2

ROOT = Path(__file__).resolve().parents[1]
CONTEXT_DIR = ROOT / "context"

# allow `from lib.context import parse_context_md` when run from anywhere
sys.path.insert(0, str(ROOT))
from lib.context import parse_context_md  # noqa: E402


def _database_url() -> str:
    v = os.environ.get("DATABASE_URL")
    if v:
        return v
    env = ROOT / ".env"
    if env.is_file():
        for line in env.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, val = line.split("=", 1)
                if k == "DATABASE_URL":
                    return val.strip()
    sys.exit("DATABASE_URL not set (env or .env)")


def main() -> int:
    force = "--force" in sys.argv[1:]
    dry_run = "--dry-run" in sys.argv[1:]

    files = sorted(glob.glob(str(CONTEXT_DIR / "*.context.md")))
    if not files:
        sys.exit(f"no context files under {CONTEXT_DIR}")

    conn = psycopg2.connect(_database_url())
    try:
        cur = conn.cursor()

        # Become a maintainer for this transaction so maintainer_set_profile_context() passes.
        cur.execute(
            """SELECT fm.auth_user_id
                 FROM family_memberships fm
                 JOIN profiles p ON p.id = fm.profile_id
                WHERE p.is_maintainer = true
                ORDER BY fm.auth_user_id
                LIMIT 1"""
        )
        row = cur.fetchone()
        if not row:
            sys.exit("no maintainer membership found — cannot authorize the backfill")
        maint_uid = str(row[0])
        cur.execute(
            "SELECT set_config('request.jwt.claims', %s, true)",
            (json.dumps({"sub": maint_uid, "role": "authenticated"}),),
        )

        seeded, skipped, missing = 0, 0, 0
        for path in files:
            body = Path(path).read_text(encoding="utf-8")
            ctx = parse_context_md(body)
            pid = ctx.get("profile_id")
            name = ctx.get("name") or Path(path).stem
            if not pid:
                print(f"  SKIP {Path(path).name}: no profile_id in header")
                missing += 1
                continue

            cur.execute("SELECT context_md FROM profiles WHERE id = %s", (pid,))
            prow = cur.fetchone()
            if prow is None:
                print(f"  SKIP {name} ({pid}): no such profile")
                missing += 1
                continue
            if prow[0] and not force:
                print(f"  skip {name} ({pid}): context_md already set ({len(prow[0])} chars)")
                skipped += 1
                continue

            if dry_run:
                print(f"  DRY-RUN would seed {name} ({pid}): {len(body)} chars")
                seeded += 1
                continue

            cur.execute(
                "SELECT maintainer_set_profile_context(%s, %s, %s)",
                (pid, body, "backfill 073 from context/*.context.md"),
            )
            new_v = cur.fetchone()[0]
            print(f"  SET {name} ({pid}): {len(body)} chars → context_version={new_v}")
            seeded += 1

        if dry_run:
            conn.rollback()
            print(f"\nDRY-RUN: would seed {seeded}, skip {skipped}, missing {missing} (rolled back)")
        else:
            conn.commit()
            print(f"\nDONE: seeded {seeded}, skipped {skipped}, missing {missing} (committed)")
        return 0
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
