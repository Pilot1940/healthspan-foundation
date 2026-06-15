"""
Integration tests for the DB-backed per-profile context write path (migration 073).

Asserts, against the live DB in rolled-back transactions:
  * maintainer_set_profile_context() bumps context_version + stamps context_updated_at,
  * a non-maintainer caller is rejected ('maintainer-only'),
  * a too-short body (<50 chars) is rejected,
  * the BEFORE-UPDATE trigger blocks a DIRECT UPDATE of context_md by a non-maintainer
    (defence-in-depth — Postgres has no column-level RLS and profiles_access is FOR ALL).

Simulates the authenticated session the way PostgREST does: SET ROLE authenticated +
request.jwt.claims with the caller's auth uid as `sub`. The maintainer uid is resolved at
runtime from family_memberships (mirrors is_maintainer's own resolution), so no uid is baked
in. Nothing is committed — every test rolls back.

Required env (skipped silently when absent):
    DATABASE_URL — from .env (psycopg2)

Run:  python3 -m pytest tests/integration/test_maintainer_set_profile_context.py -v
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

# Dea — the non-maintainer profile + her auth.users id (the JWT `sub`).
_DEA_AUTH_UID = "47501376-6adb-458e-8679-57a4a4176692"
_DEA_PROFILE_ID = "3eed5503-a26f-4b88-bb76-075208fa5de3"

_LONG_BODY = (
    "# Test — HealthSpan Context\nprofile_id: " + _DEA_PROFILE_ID + "\n\n"
    "## Targets / norms\n- daily_calories: 2500\n- protein_g: 105\n"
    "## Safety constraints\n- integration-test body, never committed.\n"
)


def _maintainer_uid(cur) -> str:
    cur.execute(
        """SELECT fm.auth_user_id FROM family_memberships fm
             JOIN profiles p ON p.id = fm.profile_id
            WHERE p.is_maintainer = true LIMIT 1"""
    )
    row = cur.fetchone()
    assert row, "no maintainer membership found in the DB"
    return str(row[0])


def _become(cur, uid: str) -> None:
    cur.execute("RESET ROLE;")
    cur.execute("SET ROLE authenticated;")
    cur.execute(
        "SELECT set_config('request.jwt.claims', %s, false)",
        ('{"sub":"%s","role":"authenticated"}' % uid,),
    )


def test_maintainer_set_bumps_version_and_stamps():
    conn = psycopg2.connect(DATABASE_URL)
    try:
        with conn.cursor() as cur:
            maint = _maintainer_uid(cur)
            _become(cur, maint)
            cur.execute("SELECT context_version FROM profiles WHERE id = %s", (_DEA_PROFILE_ID,))
            before = cur.fetchone()[0]
            cur.execute(
                "SELECT maintainer_set_profile_context(%s, %s, %s)",
                (_DEA_PROFILE_ID, _LONG_BODY, "integration test"),
            )
            new_version = cur.fetchone()[0]
            assert new_version == before + 1, "version must bump by exactly 1"
            cur.execute(
                "SELECT context_md, context_updated_at FROM profiles WHERE id = %s",
                (_DEA_PROFILE_ID,),
            )
            md, updated_at = cur.fetchone()
            assert md == _LONG_BODY, "context_md must hold the new body"
            assert updated_at is not None, "context_updated_at must be stamped"
            cur.execute("RESET ROLE;")
    finally:
        conn.rollback()
        conn.close()


def test_non_maintainer_rpc_rejected():
    conn = psycopg2.connect(DATABASE_URL)
    try:
        with conn.cursor() as cur:
            _become(cur, _DEA_AUTH_UID)
            with pytest.raises(psycopg2.Error) as ei:
                cur.execute(
                    "SELECT maintainer_set_profile_context(%s, %s)",
                    (_DEA_PROFILE_ID, _LONG_BODY),
                )
            assert "maintainer-only" in str(ei.value)
    finally:
        conn.rollback()
        conn.close()


def test_short_body_rejected():
    conn = psycopg2.connect(DATABASE_URL)
    try:
        with conn.cursor() as cur:
            _become(cur, _maintainer_uid(cur))
            with pytest.raises(psycopg2.Error) as ei:
                cur.execute(
                    "SELECT maintainer_set_profile_context(%s, %s)",
                    (_DEA_PROFILE_ID, "too short"),
                )
            assert "too short" in str(ei.value)
    finally:
        conn.rollback()
        conn.close()


def test_trigger_blocks_direct_update_by_non_maintainer():
    conn = psycopg2.connect(DATABASE_URL)
    try:
        with conn.cursor() as cur:
            _become(cur, _DEA_AUTH_UID)
            with pytest.raises(psycopg2.Error) as ei:
                cur.execute(
                    "UPDATE profiles SET context_md = %s WHERE id = %s",
                    (_LONG_BODY, _DEA_PROFILE_ID),
                )
            assert "maintainer-only" in str(ei.value)
    finally:
        conn.rollback()
        conn.close()
