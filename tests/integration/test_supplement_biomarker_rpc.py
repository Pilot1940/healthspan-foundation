"""
Integration tests for maintainer_ingest_supplement and maintainer_ingest_biomarker RPCs.

Exercises the real INSERT path (force_stage=False) and the staging path (force_stage=True)
against the live Supabase DB.

Required environment variables (skipped silently when absent):
    SUPABASE_URL       — from .env
    SUPABASE_ANON_KEY  — from .env
    DATABASE_URL       — from .env (psycopg2, for row-level assertions and cleanup)
    HS_AUTH_EMAIL      — drain service account (healthspan.drainer@chitalkar.com)
    HS_AUTH_PASSWORD   — drain service account password

Run:
    HS_AUTH_EMAIL=... HS_AUTH_PASSWORD=... python3 -m pytest tests/integration/ -v
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import httpx
import psycopg2
import pytest

# ── credentials ───────────────────────────────────────────────────────────────

def _load_dotenv() -> dict[str, str]:
    p = Path(__file__).parents[2] / ".env"
    result: dict[str, str] = {}
    if not p.exists():
        return result
    for line in p.read_text().splitlines():
        m = re.match(r"([A-Z_][A-Z0-9_]*)\s*=\s*(.+)", line.strip())
        if m:
            result[m.group(1)] = m.group(2).strip().strip("\"'")
    return result


_DOTENV = _load_dotenv()


def _cfg(key: str) -> str | None:
    return os.environ.get(key) or _DOTENV.get(key)


SUPABASE_URL = _cfg("SUPABASE_URL")
SUPABASE_ANON_KEY = _cfg("SUPABASE_ANON_KEY")
DATABASE_URL = _cfg("DATABASE_URL")
HS_AUTH_EMAIL = _cfg("HS_AUTH_EMAIL")
HS_AUTH_PASSWORD = _cfg("HS_AUTH_PASSWORD")

_MISSING = [
    k for k, v in {
        "SUPABASE_URL": SUPABASE_URL,
        "SUPABASE_ANON_KEY": SUPABASE_ANON_KEY,
        "DATABASE_URL": DATABASE_URL,
        "HS_AUTH_EMAIL": HS_AUTH_EMAIL,
        "HS_AUTH_PASSWORD": HS_AUTH_PASSWORD,
    }.items()
    if not v
]

pytestmark = pytest.mark.skipif(
    bool(_MISSING),
    reason=f"live DB credentials not set: {', '.join(_MISSING)}",
)

# ── known stable IDs ──────────────────────────────────────────────────────────

_PC_PROFILE_ID = "21f69003-46f8-4e1c-a928-b1f694ce4aff"
_BERBERINE_SUPPLEMENT_ID = "84663575-15f2-4d8c-b138-4da3234f70a9"


# ── helpers ───────────────────────────────────────────────────────────────────

def _sign_in() -> str:
    r = httpx.post(
        f"{SUPABASE_URL.rstrip('/')}/auth/v1/token",
        params={"grant_type": "password"},
        headers={"apikey": SUPABASE_ANON_KEY, "Content-Type": "application/json"},
        json={"email": HS_AUTH_EMAIL, "password": HS_AUTH_PASSWORD},
        timeout=30.0,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def _rpc(jwt: str, name: str, args: dict) -> dict:
    r = httpx.post(
        f"{SUPABASE_URL.rstrip('/')}/rest/v1/rpc/{name}",
        headers={
            "apikey": SUPABASE_ANON_KEY,
            "Authorization": f"Bearer {jwt}",
            "Content-Type": "application/json",
        },
        json=args,
        timeout=30.0,
    )
    r.raise_for_status()
    return r.json()


def _db_read(sql: str, params: tuple = ()) -> list[tuple]:
    conn = psycopg2.connect(DATABASE_URL)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()
    finally:
        conn.close()


def _db_delete(sql: str, params: tuple = ()) -> None:
    conn = psycopg2.connect(DATABASE_URL)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


# ── fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def jwt() -> str:
    return _sign_in()


@pytest.fixture(scope="module")
def metric_id() -> str:
    rows = _db_read("SELECT id FROM metric_definitions WHERE is_active = true LIMIT 1")
    assert rows, "no active metric_definitions in DB"
    return str(rows[0][0])


# ── supplement RPC ────────────────────────────────────────────────────────────

class TestSupplementRPC:
    """maintainer_ingest_supplement — INSERT path and staging path."""

    def test_force_stage_false_inserts_and_taken_on_populated(self, jwt: str) -> None:
        """force_stage=False → row written to supplement_intake_logs; taken_on auto-populated
        by the GENERATED ALWAYS expression (this is the 428C9 regression test).

        NOTE: supplement_intake_logs.source has a CHECK constraint allowing only
        'manual', 'journal', 'skill', 'csv', 'photo'. The drain uses 'telegram'
        which would fail this constraint on the INSERT path — a separate bug tracked
        outside this test (see supplement_intake_logs_source_check). We use 'manual'
        here to exercise the INSERT path and verify the GENERATED column fix.
        """
        taken_at = "2020-01-15T08:00:00+00:00"
        row_id = None
        try:
            result = _rpc(jwt, "maintainer_ingest_supplement", {
                "p_profile_id":    _PC_PROFILE_ID,
                "p_supplement_id": _BERBERINE_SUPPLEMENT_ID,
                "p_dose_amount":   500,
                "p_dose_unit":     "mg",
                "p_taken_at":      taken_at,
                "p_source":        "manual",  # "telegram" fails the source CHECK constraint
                "p_notes":         "039_taken_on_regression",
                "p_confidence":    0.9,
                "p_force_stage":   False,
                "p_stage_reason":  None,
            })
            row_id = result.get("id")
            assert result.get("status") == "inserted", \
                f"expected 'inserted', got: {result!r}"

            # Verify taken_on was auto-populated by the GENERATED ALWAYS expression
            rows = _db_read(
                "SELECT taken_on FROM supplement_intake_logs WHERE id = %s",
                (row_id,),
            )
            assert rows, "inserted row not found in supplement_intake_logs"
            taken_on = rows[0][0]
            assert taken_on is not None, \
                "taken_on is NULL — GENERATED column not auto-populated (428C9 regression)"
            assert str(taken_on) == "2020-01-15", \
                f"unexpected taken_on value: {taken_on!r}"
        finally:
            if row_id:
                _db_delete(
                    "DELETE FROM supplement_intake_logs WHERE id = %s",
                    (row_id,),
                )

    def test_force_stage_true_writes_staging_row(self, jwt: str) -> None:
        """force_stage=True → row in stg_supplement_intake_review with stage_reason."""
        row_id = None
        try:
            result = _rpc(jwt, "maintainer_ingest_supplement", {
                "p_profile_id":     _PC_PROFILE_ID,
                "p_supplement_id":  None,
                "p_extracted_name": "unknown herb",
                "p_dose_amount":    250,
                "p_dose_unit":      "mg",
                "p_confidence":     0.5,
                "p_force_stage":    True,
                "p_stage_reason":   "integration_test: no supplement match",
            })
            row_id = result.get("id")
            assert result.get("status") == "staged", \
                f"expected 'staged', got: {result!r}"
            assert result.get("stage_reason") == "integration_test: no supplement match"

            rows = _db_read(
                "SELECT stage_reason FROM stg_supplement_intake_review WHERE id = %s",
                (row_id,),
            )
            assert rows, "staging row not found in stg_supplement_intake_review"
            assert rows[0][0] == "integration_test: no supplement match"
        finally:
            if row_id:
                _db_delete(
                    "DELETE FROM stg_supplement_intake_review WHERE id = %s",
                    (row_id,),
                )


# ── biomarker RPC ─────────────────────────────────────────────────────────────

class TestBiomarkerRPC:
    """maintainer_ingest_biomarker — INSERT path and staging path."""

    def test_force_stage_false_inserts_row(self, jwt: str, metric_id: str) -> None:
        """force_stage=False → row written to biomarkers (no generated columns)."""
        measured_at = "2020-02-01T06:00:00+00:00"
        row_id = None
        try:
            result = _rpc(jwt, "maintainer_ingest_biomarker", {
                "p_profile_id":           _PC_PROFILE_ID,
                "p_metric_definition_id": metric_id,
                "p_value":                99.0,
                "p_unit":                 "mg/dL",
                "p_measured_at":          measured_at,
                "p_source":               "integration_test",
                "p_notes":                "039_biomarker_insert",
                "p_extracted_name":       "glucose",
                "p_confidence":           0.95,
                "p_force_stage":          False,
                "p_stage_reason":         None,
            })
            row_id = result.get("id")
            assert result.get("status") == "inserted", \
                f"expected 'inserted', got: {result!r}"

            rows = _db_read(
                "SELECT value, source FROM biomarkers WHERE id = %s",
                (row_id,),
            )
            assert rows, "inserted row not found in biomarkers"
            assert float(rows[0][0]) == 99.0
        finally:
            if row_id:
                _db_delete("DELETE FROM biomarkers WHERE id = %s", (row_id,))

    def test_force_stage_true_writes_staging_row(self, jwt: str) -> None:
        """force_stage=True → row in stg_biomarker_review with stage_reason."""
        row_id = None
        try:
            result = _rpc(jwt, "maintainer_ingest_biomarker", {
                "p_profile_id":     _PC_PROFILE_ID,
                "p_extracted_name": "unknown_marker",
                "p_value":          5.5,
                "p_unit":           "mmol/L",
                "p_confidence":     0.4,
                "p_force_stage":    True,
                "p_stage_reason":   "integration_test: no metric match",
            })
            row_id = result.get("id")
            assert result.get("status") == "staged", \
                f"expected 'staged', got: {result!r}"

            rows = _db_read(
                "SELECT stage_reason FROM stg_biomarker_review WHERE id = %s",
                (row_id,),
            )
            assert rows, "staging row not found in stg_biomarker_review"
            assert rows[0][0] == "integration_test: no metric match"
        finally:
            if row_id:
                _db_delete("DELETE FROM stg_biomarker_review WHERE id = %s", (row_id,))
