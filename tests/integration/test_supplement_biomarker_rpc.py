"""
Integration tests for maintainer_ingest_supplement, maintainer_ingest_biomarker,
and maintainer_ingest_food RPCs.

Exercises the real INSERT path (force_stage=False) and the staging path (force_stage=True)
against the live Supabase DB.

Tests prefixed _039_ verify the 428C9 GENERATED-column regression fix.
Tests prefixed _040_ verify the write-contract audit fixes — specifically that each RPC
accepts source='telegram' (the actual drain source) and writes rows to the real tables.

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

    def test_039_taken_on_generated_column_not_inserted(self, jwt: str) -> None:
        """force_stage=False, source='manual' → INSERT succeeds; taken_on auto-populated.
        Regression test for 428C9 (GENERATED ALWAYS column mistakenly included in INSERT).
        Uses 'manual' source to isolate the GENERATED column fix from the source CHECK fix.
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
                "p_source":        "manual",
                "p_notes":         "039_taken_on_regression",
                "p_confidence":    0.9,
                "p_force_stage":   False,
                "p_stage_reason":  None,
            })
            row_id = result.get("id")
            assert result.get("status") == "inserted", \
                f"expected 'inserted', got: {result!r}"

            rows = _db_read(
                "SELECT taken_on, source FROM supplement_intake_logs WHERE id = %s",
                (row_id,),
            )
            assert rows, "inserted row not found in supplement_intake_logs"
            taken_on, source = rows[0]
            assert taken_on is not None, \
                "taken_on is NULL — GENERATED column not auto-populated (428C9 regression)"
            assert str(taken_on) == "2020-01-15", \
                f"unexpected taken_on value: {taken_on!r}"
            assert source == "manual"
        finally:
            if row_id:
                _db_delete(
                    "DELETE FROM supplement_intake_logs WHERE id = %s",
                    (row_id,),
                )

    def test_040_telegram_source_inserts_real_row(self, jwt: str) -> None:
        """force_stage=False, source='telegram' → INSERT succeeds (040 adds telegram to CHECK).
        This is the test that would have caught the source CHECK gap before 040.
        """
        taken_at = "2020-03-10T07:00:00+00:00"
        row_id = None
        try:
            result = _rpc(jwt, "maintainer_ingest_supplement", {
                "p_profile_id":    _PC_PROFILE_ID,
                "p_supplement_id": _BERBERINE_SUPPLEMENT_ID,
                "p_dose_amount":   500,
                "p_dose_unit":     "mg",
                "p_taken_at":      taken_at,
                "p_source":        "telegram",
                "p_notes":         "040_source_check_fix",
                "p_confidence":    0.9,
                "p_force_stage":   False,
                "p_stage_reason":  None,
            })
            row_id = result.get("id")
            assert result.get("status") == "inserted", \
                f"expected 'inserted', got: {result!r}"

            rows = _db_read(
                "SELECT taken_on, source FROM supplement_intake_logs WHERE id = %s",
                (row_id,),
            )
            assert rows, "inserted row not found in supplement_intake_logs"
            taken_on, source = rows[0]
            assert str(taken_on) == "2020-03-10", \
                f"unexpected taken_on: {taken_on!r}"
            assert source == "telegram", \
                f"unexpected source: {source!r}"
        finally:
            if row_id:
                _db_delete(
                    "DELETE FROM supplement_intake_logs WHERE id = %s",
                    (row_id,),
                )

    def test_040_null_supplement_id_db_guard_stages(self, jwt: str) -> None:
        """supplement_id=None + force_stage=False → DB guard stages with reason.
        Tests the 040 belt-and-suspenders guard (Python completeness gate is the first line).
        """
        row_id = None
        try:
            result = _rpc(jwt, "maintainer_ingest_supplement", {
                "p_profile_id":     _PC_PROFILE_ID,
                "p_supplement_id":  None,
                "p_extracted_name": "unresolved herb",
                "p_dose_amount":    200,
                "p_dose_unit":      "mg",
                "p_confidence":     0.8,
                "p_force_stage":    False,
                "p_stage_reason":   None,
            })
            row_id = result.get("id")
            assert result.get("status") == "staged", \
                f"expected 'staged' (DB guard should fire), got: {result!r}"
            assert result.get("stage_reason") == "no supplement_id: required for INSERT", \
                f"unexpected stage_reason: {result.get('stage_reason')!r}"

            rows = _db_read(
                "SELECT stage_reason FROM stg_supplement_intake_review WHERE id = %s",
                (row_id,),
            )
            assert rows, "staging row not found"
            assert rows[0][0] == "no supplement_id: required for INSERT"
        finally:
            if row_id:
                _db_delete(
                    "DELETE FROM stg_supplement_intake_review WHERE id = %s",
                    (row_id,),
                )

    def test_force_stage_true_writes_staging_row(self, jwt: str) -> None:
        """Explicit force_stage=True → row in stg_supplement_intake_review with stage_reason."""
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

    def test_040_telegram_source_inserts_real_row(self, jwt: str, metric_id: str) -> None:
        """force_stage=False, source='telegram' → INSERT succeeds (no CHECK on biomarkers.source).
        This is the test that would have caught any source constraint gap.
        """
        measured_at = "2020-02-15T06:00:00+00:00"
        row_id = None
        try:
            result = _rpc(jwt, "maintainer_ingest_biomarker", {
                "p_profile_id":           _PC_PROFILE_ID,
                "p_metric_definition_id": metric_id,
                "p_value":                98.0,
                "p_unit":                 "mg/dL",
                "p_measured_at":          measured_at,
                "p_source":               "telegram",
                "p_notes":                "040_telegram_source",
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
            assert float(rows[0][0]) == 98.0
            assert rows[0][1] == "telegram", f"unexpected source: {rows[0][1]!r}"
        finally:
            if row_id:
                _db_delete("DELETE FROM biomarkers WHERE id = %s", (row_id,))

    def test_040_null_metric_id_db_guard_stages(self, jwt: str) -> None:
        """metric_definition_id=None + force_stage=False → DB guard stages with reason."""
        row_id = None
        try:
            result = _rpc(jwt, "maintainer_ingest_biomarker", {
                "p_profile_id":     _PC_PROFILE_ID,
                "p_extracted_name": "unresolved marker",
                "p_value":          5.5,
                "p_unit":           "mmol/L",
                "p_confidence":     0.7,
                "p_force_stage":    False,
                "p_stage_reason":   None,
            })
            row_id = result.get("id")
            assert result.get("status") == "staged", \
                f"expected 'staged' (DB guard should fire), got: {result!r}"
            assert "no metric_id" in (result.get("stage_reason") or ""), \
                f"unexpected stage_reason: {result.get('stage_reason')!r}"
        finally:
            if row_id:
                _db_delete(
                    "DELETE FROM stg_biomarker_review WHERE id = %s",
                    (row_id,),
                )

    def test_039_force_stage_false_inserts_row(self, jwt: str, metric_id: str) -> None:
        """Regression test (039): force_stage=False → row written to biomarkers."""
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


# ── food RPC ──────────────────────────────────────────────────────────────────

class TestFoodRPC:
    """maintainer_ingest_food — INSERT path and staging path."""

    def test_040_telegram_source_inserts_real_row(self, jwt: str) -> None:
        """force_stage=False, source='telegram' → INSERT succeeds (food_logs has no source CHECK).
        This is the test that would have caught any source constraint gap on food.
        """
        logged_at = "2020-04-01T12:00:00+00:00"
        row_id = None
        try:
            result = _rpc(jwt, "maintainer_ingest_food", {
                "p_profile_id":  _PC_PROFILE_ID,
                "p_meal_type":   "lunch",
                "p_description": "grilled chicken + rice (040 integration test)",
                "p_calories":    600,
                "p_protein_g":   45,
                "p_carbs_g":     60,
                "p_fat_g":       12,
                "p_logged_at":   logged_at,
                "p_source":      "telegram",
                "p_confidence":  0.9,
                "p_force_stage": False,
                "p_stage_reason": None,
            })
            row_id = result.get("id")
            assert result.get("status") == "inserted", \
                f"expected 'inserted', got: {result!r}"

            rows = _db_read(
                "SELECT description, source, log_date FROM food_logs WHERE id = %s",
                (row_id,),
            )
            assert rows, "inserted row not found in food_logs"
            desc, source, log_date = rows[0]
            assert "grilled chicken" in desc
            assert source == "telegram", f"unexpected source: {source!r}"
            assert str(log_date) == "2020-04-01", f"unexpected log_date: {log_date!r}"
        finally:
            if row_id:
                _db_delete("DELETE FROM food_logs WHERE id = %s", (row_id,))

    def test_040_invalid_meal_type_db_guard_stages(self, jwt: str) -> None:
        """meal_type='unknown' + force_stage=False → DB guard stages with reason.
        food_logs_meal_type_check does not allow 'unknown'; the food vision prompt
        previously offered it as an option (fixed in 040 Python change).
        """
        row_id = None
        try:
            result = _rpc(jwt, "maintainer_ingest_food", {
                "p_profile_id":  _PC_PROFILE_ID,
                "p_meal_type":   "unknown",
                "p_description": "mystery meal (040 meal_type guard test)",
                "p_calories":    400,
                "p_protein_g":   20,
                "p_logged_at":   "2020-04-02T12:00:00+00:00",
                "p_source":      "telegram",
                "p_confidence":  0.6,
                "p_force_stage": False,
                "p_stage_reason": None,
            })
            row_id = result.get("id")
            assert result.get("status") == "staged", \
                f"expected 'staged' (DB guard should fire), got: {result!r}"
            assert "invalid meal_type" in (result.get("stage_reason") or ""), \
                f"unexpected stage_reason: {result.get('stage_reason')!r}"
            assert "unknown" in (result.get("stage_reason") or ""), \
                f"stage_reason should mention the bad value: {result.get('stage_reason')!r}"
        finally:
            if row_id:
                _db_delete("DELETE FROM stg_food_log_review WHERE id = %s", (row_id,))

    def test_040_null_description_db_guard_stages(self, jwt: str) -> None:
        """description=None + force_stage=False → DB guard stages with reason."""
        row_id = None
        try:
            result = _rpc(jwt, "maintainer_ingest_food", {
                "p_profile_id":  _PC_PROFILE_ID,
                "p_meal_type":   "lunch",
                "p_description": None,
                "p_calories":    400,
                "p_protein_g":   20,
                "p_logged_at":   "2020-04-03T12:00:00+00:00",
                "p_source":      "telegram",
                "p_confidence":  0.5,
                "p_force_stage": False,
                "p_stage_reason": None,
            })
            row_id = result.get("id")
            assert result.get("status") == "staged", \
                f"expected 'staged' (DB guard should fire), got: {result!r}"
            assert "description" in (result.get("stage_reason") or ""), \
                f"stage_reason should mention description: {result.get('stage_reason')!r}"
        finally:
            if row_id:
                _db_delete("DELETE FROM stg_food_log_review WHERE id = %s", (row_id,))


# ── sprint_set_adherence RPC (mig 066 workout keys + mig 068 nutrition keys) ────

class TestSprintAdherenceRPC:
    """sprint_set_adherence — verifies mig 068 widened the activity allow-list to include
    the food micronutrient keys (iron/calcium/vitamin_d) alongside the mig-066 workout keys,
    and still rejects anything else. Writes under a sentinel far-past date and cleans up, so
    no real adherence data is touched."""

    _SENTINEL_DATE = "2020-01-01"  # no real adherence_log entry exists this far back

    @pytest.fixture(scope="class")
    def pc_sprint_id(self) -> str:
        # MUST be an object-goals sprint: sprint_set_adherence does jsonb_set on
        # goals.adherence_log, which RAISES on a legacy FLAT-ARRAY goals row (the pre-v3.17
        # shape). Production only ever ticks the ACTIVE sprint (object form), so pick the most
        # recent object-goals sprint — never the oldest, which may be a legacy array.
        rows = _db_read(
            "SELECT id FROM sprints WHERE profile_id = %s AND jsonb_typeof(goals) = 'object' "
            "ORDER BY start_date DESC LIMIT 1",
            (_PC_PROFILE_ID,),
        )
        if not rows:
            pytest.skip("no object-goals sprint owned by PC to exercise sprint_set_adherence")
        return str(rows[0][0])

    def _cleanup(self, sprint_id: str) -> None:
        # Remove the whole sentinel-date object so the test leaves no trace.
        _db_delete(
            "UPDATE sprints SET goals = goals #- %s WHERE id = %s",
            ("{adherence_log,%s}" % self._SENTINEL_DATE, sprint_id),
        )

    def test_mig068_accepts_nutrition_and_workout_keys(self, jwt: str, pc_sprint_id: str) -> None:
        """iron/calcium/vitamin_d (mig 068) + a workout key (mig 066) all write into the same
        adherence_log[date] object and read back true."""
        try:
            day = {}
            for activity in ("iron", "calcium", "vitamin_d", "gym"):
                day = _rpc(jwt, "sprint_set_adherence", {
                    "p_sprint_id":  pc_sprint_id,
                    "p_date":       self._SENTINEL_DATE,
                    "p_activity":   activity,
                    "p_value":      True,
                    "p_profile_id": _PC_PROFILE_ID,
                })
            # The RPC returns goals.adherence_log[date]; all four keys must be present + true.
            assert day.get("iron") is True, f"iron not accepted (mig 068): {day!r}"
            assert day.get("calcium") is True, f"calcium not accepted (mig 068): {day!r}"
            assert day.get("vitamin_d") is True, f"vitamin_d not accepted (mig 068): {day!r}"
            assert day.get("gym") is True, f"workout key regressed (mig 066): {day!r}"

            # Confirm it actually persisted on the row (not just the RETURNING value).
            rows = _db_read(
                "SELECT goals #> %s FROM sprints WHERE id = %s",
                ("{adherence_log,%s}" % self._SENTINEL_DATE, pc_sprint_id),
            )
            assert rows and rows[0][0] and rows[0][0].get("iron") is True
        finally:
            self._cleanup(pc_sprint_id)

    def test_mig068_rejects_unknown_activity(self, jwt: str, pc_sprint_id: str) -> None:
        """An activity outside the allow-list still RAISEs (the validation wasn't just removed)."""
        with pytest.raises(httpx.HTTPStatusError):
            _rpc(jwt, "sprint_set_adherence", {
                "p_sprint_id":  pc_sprint_id,
                "p_date":       self._SENTINEL_DATE,
                "p_activity":   "banana",
                "p_value":      True,
                "p_profile_id": _PC_PROFILE_ID,
            })
