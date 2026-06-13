"""
Integration test for the view-level RLS isolation fix (migration 067).

A view without `security_invoker = true` runs as its owner and BYPASSES the base
tables' row-level security. `daily_health_summary` had this flaw and leaked every
profile's rows (and `daily_supplement_outcomes` inherited it). This test asserts
that, under a non-maintainer's authenticated JWT, every public view exposing
profile_id returns exactly ONE profile_id — the caller's own.

Simulates the authenticated session the way PostgREST does: SET ROLE authenticated
+ request.jwt.claims with the user's auth uid as `sub`, so the RLS policies resolve
to that profile.

Required env (skipped silently when absent):
    DATABASE_URL — from .env (psycopg2)

Run:  python3 -m pytest tests/integration/test_rls_view_isolation.py -v
"""
from __future__ import annotations

import os
from pathlib import Path

import psycopg2
import pytest


def _cfg(key: str) -> str | None:
    v = os.environ.get(key)
    if v:
        return v
    env = Path(__file__).resolve().parents[2] / ".env"
    if env.is_file():
        for line in env.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, val = line.split("=", 1)
                if k == key:
                    return val.strip()
    return None


DATABASE_URL = _cfg("DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="live DB credential not set: DATABASE_URL"
)

# Dea — non-maintainer profile + her auth.users id (the JWT `sub`).
_DEA_AUTH_UID = "47501376-6adb-458e-8679-57a4a4176692"
_DEA_PROFILE_ID = "3eed5503-a26f-4b88-bb76-075208fa5de3"

# Views that expose profile_id and therefore MUST be RLS-scoped under the caller's JWT.
_PROFILE_VIEWS = [
    "daily_health_summary",
    "daily_supplement_outcomes",
    "effective_food_guidance",
    "resolved_supplement_exposure",
    "supplement_exposure_daily",
    "v_india_vs_travel",
    "whoop_journal",
]


def _as_dea_count(cur, relation: str) -> int:
    """count(DISTINCT profile_id) of `relation` under Dea's authenticated JWT."""
    cur.execute("RESET ROLE;")
    cur.execute("SET ROLE authenticated;")
    cur.execute(
        "SELECT set_config('request.jwt.claims', %s, false)",
        ('{"sub":"%s","role":"authenticated"}' % _DEA_AUTH_UID,),
    )
    cur.execute(f"SELECT count(DISTINCT profile_id) FROM {relation}")
    n = cur.fetchone()[0]
    cur.execute("RESET ROLE;")
    return n


def test_base_table_is_rls_scoped():
    """Sanity: the base table is correctly scoped to Dea only (the control)."""
    conn = psycopg2.connect(DATABASE_URL)
    try:
        with conn.cursor() as cur:
            assert _as_dea_count(cur, "whoop_cycles") == 1
    finally:
        conn.rollback()
        conn.close()


def test_daily_health_summary_no_cross_profile_leak():
    """The previously-leaking view must now return only Dea's own profile."""
    conn = psycopg2.connect(DATABASE_URL)
    try:
        with conn.cursor() as cur:
            assert _as_dea_count(cur, "daily_health_summary") == 1
            # and it must be HER profile, not someone else's
            cur.execute("SET ROLE authenticated;")
            cur.execute(
                "SELECT set_config('request.jwt.claims', %s, false)",
                ('{"sub":"%s","role":"authenticated"}' % _DEA_AUTH_UID,),
            )
            cur.execute("SELECT DISTINCT profile_id FROM daily_health_summary")
            pids = {str(r[0]) for r in cur.fetchall()}
            cur.execute("RESET ROLE;")
            assert pids <= {_DEA_PROFILE_ID}, f"leaked profiles: {pids}"
    finally:
        conn.rollback()
        conn.close()


def test_all_profile_views_rls_scoped():
    """Every view exposing profile_id returns at most the caller's own profile."""
    conn = psycopg2.connect(DATABASE_URL)
    try:
        with conn.cursor() as cur:
            for v in _PROFILE_VIEWS:
                assert _as_dea_count(cur, v) <= 1, f"{v} leaks cross-profile rows"
    finally:
        conn.rollback()
        conn.close()


def test_all_profile_views_have_security_invoker():
    """Structural guard: no profile_id view may lack security_invoker=true."""
    conn = psycopg2.connect(DATABASE_URL)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT c.relname
                  FROM pg_class c
                  JOIN pg_namespace n ON n.oid = c.relnamespace
                 WHERE n.nspname = 'public' AND c.relkind = 'v'
                   AND EXISTS (SELECT 1 FROM information_schema.columns col
                                WHERE col.table_schema='public'
                                  AND col.table_name=c.relname
                                  AND col.column_name='profile_id')
                   AND NOT (coalesce(array_to_string(c.reloptions, ','), '')
                            ILIKE '%%security_invoker=true%%')
                """
            )
            offenders = [r[0] for r in cur.fetchall()]
            assert not offenders, f"views missing security_invoker: {offenders}"
    finally:
        conn.rollback()
        conn.close()
