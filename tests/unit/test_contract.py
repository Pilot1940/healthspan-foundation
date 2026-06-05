"""Unit tests for lib/contract.py — all DB calls mocked.

Proves three contract invariants:
  1. write() is idempotent — same natural key = UPDATE (not second INSERT)
  2. A bad row (validation failure) is logged, run continues (not raised)
  3. An out-of-range biomarker value is routed to staging, never coerced
"""
from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import pytest

from lib.contract import (
    ContractError,
    close_sync_log,
    ingest_record,
    log_error,
    open_sync_log,
    stage,
    validate,
    write,
)


# ---------------------------------------------------------------------------
# Helpers — build lightweight mock cursors / connections
# ---------------------------------------------------------------------------

PROFILE_UUID = str(uuid.uuid4())
METRIC_UUID = str(uuid.uuid4())
SUPPLEMENT_UUID = str(uuid.uuid4())


def _mock_conn(*cursor_sequence):
    """Return a mock conn whose cursor() calls return cursors in order."""
    conn = MagicMock()
    cursors = [_make_cursor(seq) for seq in cursor_sequence]
    conn.cursor.side_effect = cursors
    return conn


def _make_cursor(fetchone_results: list):
    """A cursor whose fetchone() pops from a list, fetchall() returns []."""
    cur = MagicMock()
    results = list(fetchone_results)

    def _fetchone():
        return results.pop(0) if results else None

    cur.fetchone.side_effect = _fetchone
    cur.fetchall.return_value = []
    return cur


def _conn_with_config(threshold=0.7, extra_cursors=None):
    """Connection pre-loaded with a system_config ingest.confidence_min row."""
    conn = MagicMock()
    cur_seq = [(threshold,)]  # first cursor → confidence_min
    for seq in (extra_cursors or []):
        cur_seq.append(seq)
    cursors = [_make_cursor(s if isinstance(s, list) else [s]) for s in cur_seq]
    conn.cursor.side_effect = cursors
    return conn


# ===========================================================================
# write() — idempotency
# ===========================================================================


class TestWrite:
    def _conn_for_write(self, returning_row):
        cur = MagicMock()
        cur.fetchone.return_value = returning_row
        conn = MagicMock()
        conn.cursor.return_value = cur
        return conn, cur

    def test_insert_returns_inserted(self):
        # Include a non-conflict col (recovery_score_pct) so DO UPDATE SET is generated.
        conn, cur = self._conn_for_write((True,))  # xmax=0 → inserted
        result = write(
            conn, "whoop_cycles",
            {"profile_id": PROFILE_UUID, "cycle_start": "2026-06-01 00:00+00",
             "recovery_score_pct": 72},
            ["profile_id", "cycle_start"],
        )
        assert result == "inserted"
        sql_called = cur.execute.call_args[0][0]
        assert "ON CONFLICT" in sql_called
        assert "DO UPDATE SET" in sql_called

    def test_update_returns_updated(self):
        conn, cur = self._conn_for_write((False,))  # xmax!=0 → updated
        result = write(
            conn, "whoop_cycles",
            {"profile_id": PROFILE_UUID, "cycle_start": "2026-06-01 00:00+00",
             "recovery_score_pct": 72},
            ["profile_id", "cycle_start"],
        )
        assert result == "updated"

    def test_do_nothing_returns_skipped(self):
        """When DO NOTHING fires (duplicate with no update cols) fetchone is None."""
        conn, cur = self._conn_for_write(None)
        result = write(
            conn, "whoop_cycles",
            {"profile_id": PROFILE_UUID, "cycle_start": "2026-06-01 00:00+00"},
            ["profile_id", "cycle_start"],  # all cols are conflict cols → DO NOTHING
        )
        assert result == "skipped"

    def test_conflict_col_not_in_row_raises(self):
        conn, _ = self._conn_for_write((True,))
        with pytest.raises(ValueError, match="conflict column"):
            write(conn, "biomarkers", {"profile_id": PROFILE_UUID},
                  ["profile_id", "metric_definition_id"])

    def test_empty_row_raises(self):
        conn, _ = self._conn_for_write(None)
        with pytest.raises(ValueError, match="empty row"):
            write(conn, "biomarkers", {}, ["profile_id"])

    def test_unsafe_table_name_raises(self):
        conn, _ = self._conn_for_write(None)
        with pytest.raises(ValueError, match="unsafe SQL identifier"):
            write(conn, "'; DROP TABLE biomarkers; --",
                  {"profile_id": PROFILE_UUID}, ["profile_id"])

    def test_update_excludes_conflict_cols_from_set(self):
        conn, cur = self._conn_for_write((False,))
        write(
            conn, "biomarkers",
            {"profile_id": PROFILE_UUID, "metric_definition_id": METRIC_UUID,
             "measured_at": "2026-06-01 12:00+00", "value": 5.5},
            ["profile_id", "metric_definition_id", "measured_at"],
        )
        sql = cur.execute.call_args[0][0]
        # conflict cols must NOT appear in SET clause
        assert '"profile_id" = EXCLUDED."profile_id"' not in sql
        assert '"value" = EXCLUDED."value"' in sql


# ===========================================================================
# validate() + ingest_record() — bad row is LOGGED, run continues
# ===========================================================================


class TestBadRowLogged:
    """A row that fails validation (missing required field) must be logged via
    wearable_sync_errors and must NOT raise — the run continues."""

    # ingest_record fetchone sequence for a biomarker with `name` set:
    #   1. resolve: exact metric match  → (METRIC_UUID, "mmol/L")  [id, unit — 2 cols]
    #   2. confidence_min check         → (0.7,)
    #   3. validate FK: profile_id      → (1,)
    #   4. validate FK: metric_def      → (1,)
    #   5. validate range check         → (lo, hi, name)
    # (validate runs all checks even when required fails — no early exit)

    def test_missing_required_field_logs_and_does_not_raise(self):
        """measured_at is required for biomarkers; omitting it must log, not raise."""
        conn = MagicMock()
        cur = MagicMock()
        cur.fetchone.side_effect = [
            (METRIC_UUID, "mmol/L"),  # resolve: exact match (id, unit)
            (0.7,),                   # confidence_min (called after resolve)
            (1,),                     # validate FK: profile_id
            (1,),                     # validate FK: metric_definition_id
            (None, None, "HbA1c", 2.0, 20.0),  # clinical none; plausible 2-20 → 5.0 ok
        ]
        conn.cursor.return_value = cur

        row = {
            "profile_id": PROFILE_UUID,
            "value": 5.0,
            "name": "HbA1c",   # needed so resolve finds the metric
            "unit": "mmol/L",
            # measured_at deliberately omitted — triggers required error
        }
        result = ingest_record(
            conn, sync_log_id=42, kind="biomarker", table="biomarkers",
            raw=row, conflict_cols=["profile_id", "metric_definition_id", "measured_at"],
        )
        assert result["status"] == "failed"
        assert "errors" in result

        # wearable_sync_errors INSERT must have been called
        error_inserts = [
            c for c in cur.execute.call_args_list
            if "wearable_sync_errors" in str(c)
        ]
        assert len(error_inserts) >= 1

    def test_run_continues_after_bad_row(self):
        """Two records; first fails validation, second succeeds. No exception escapes."""
        # --- record 1: bad (missing measured_at) ---
        conn1 = MagicMock()
        cur1 = MagicMock()
        cur1.fetchone.side_effect = [
            (METRIC_UUID, "mmol/L"),  # resolve exact match
            (0.7,),                   # confidence_min
            (1,),                     # FK: profile
            (1,),                     # FK: metric_def
            (None, None, "HbA1c", 2.0, 20.0),  # plausible 2-20 → 3.0 ok
        ]
        conn1.cursor.return_value = cur1

        r1 = ingest_record(
            conn1, sync_log_id=1, kind="biomarker", table="biomarkers",
            raw={"profile_id": PROFILE_UUID, "value": 3.0, "name": "HbA1c"},
            conflict_cols=["profile_id", "metric_definition_id", "measured_at"],
        )
        assert r1["status"] == "failed"

        # --- record 2: good (all fields present) ---
        conn2 = MagicMock()
        cur2 = MagicMock()
        cur2.fetchone.side_effect = [
            (METRIC_UUID, "mmol/L"),      # resolve exact match
            (0.7,),                       # confidence_min
            (1,),                         # FK: profile
            (1,),                         # FK: metric_def
            (5.0, 10.0, "HbA1c", 2.0, 20.0),  # clinical [5,10], plausible [2,20] → 7 ok
            (True,),                      # write: RETURNING (xmax=0) → inserted
        ]
        conn2.cursor.return_value = cur2

        r2 = ingest_record(
            conn2, sync_log_id=1, kind="biomarker", table="biomarkers",
            raw={"profile_id": PROFILE_UUID, "metric_definition_id": METRIC_UUID,
                 "measured_at": "2026-06-01 09:00+00", "value": 7.0,
                 "name": "HbA1c", "unit": "mmol/L"},
            conflict_cols=["profile_id", "metric_definition_id", "measured_at"],
        )
        assert r2["status"] in ("inserted", "updated")


# ===========================================================================
# Out-of-range value → staging (never coerced, never discarded)
# ===========================================================================


class TestOutOfRangeFlagged:
    """A biomarker value outside metric_definitions.min/max must land in
    stg_biomarker_review — never silently coerced, never dropped."""

    # fetchone order for a fully-valid biomarker ingest_record call:
    #   1. resolve exact match       → (METRIC_UUID, "%")      [id, unit]
    #   2. confidence_min            → (0.7,)
    #   3. validate FK: profile      → (1,)
    #   4. validate FK: metric_def   → (1,)
    #   5. validate range            → (lo, hi, name)
    #   6. [if staged] stg INSERT    → (stg_id,)
    #   6. [if prod]   write INSERT  → (True/False,)  [xmax=0 for insert]

    def test_out_of_range_writes_to_prod_flagged(self):
        # Reference range is for FLAGGING, not gating: a confidently-read out-of-range
        # value is real data → writes to PROD with abnormal=True (NOT staged).
        conn = MagicMock()
        cur = MagicMock()
        cur.fetchone.side_effect = [
            (METRIC_UUID, "%"),      # resolve exact match (id, unit — 2 cols)
            (0.7,),                  # confidence_min
            (1,),                    # validate FK: profile
            (1,),                    # validate FK: metric_def
            (4.0, 6.0, "HbA1c", 2.0, 20.0),  # clinical [4,6]; plausible [2,20] → 12 breaches clinical only
            (True,),                 # write INSERT … ON CONFLICT RETURNING (xmax=0 → inserted)
        ]
        conn.cursor.return_value = cur

        result = ingest_record(
            conn, sync_log_id=99, kind="biomarker", table="biomarkers",
            raw={
                "profile_id": PROFILE_UUID,
                "metric_definition_id": METRIC_UUID,
                "name": "HbA1c",
                "measured_at": "2026-06-01 08:00+00",
                "value": 12.0,  # above max 6 → out_of_range, but still real
                "unit": "%",
            },
            conflict_cols=["profile_id", "metric_definition_id", "measured_at"],
        )
        assert result["status"] == "inserted"
        assert result.get("abnormal") is True
        assert any(e["code"] == "out_of_range" for e in result.get("range_flags", []))

        # MUST write to prod, and MUST NOT route to the staging table
        inserts_to_prod = [
            c for c in cur.execute.call_args_list
            if 'INSERT INTO "biomarkers"' in str(c)
        ]
        assert len(inserts_to_prod) == 1
        stage_inserts = [
            c for c in cur.execute.call_args_list
            if "stg_biomarker_review" in str(c)
        ]
        assert len(stage_inserts) == 0

    def test_value_in_range_writes_to_prod(self):
        conn = MagicMock()
        cur = MagicMock()
        cur.fetchone.side_effect = [
            (METRIC_UUID, "%"),      # resolve exact match
            (0.7,),                  # confidence_min
            (1,),                    # FK profile
            (1,),                    # FK metric_def
            (4.0, 6.0, "HbA1c", 2.0, 20.0),  # value 5.2 in clinical [4,6] + plausible [2,20]
            (True,),                 # write RETURNING (xmax=0) → inserted
        ]
        conn.cursor.return_value = cur

        result = ingest_record(
            conn, sync_log_id=99, kind="biomarker", table="biomarkers",
            raw={
                "profile_id": PROFILE_UUID,
                "metric_definition_id": METRIC_UUID,
                "name": "HbA1c",
                "measured_at": "2026-06-01 08:00+00",
                "value": 5.2,
                "unit": "%",
            },
            conflict_cols=["profile_id", "metric_definition_id", "measured_at"],
        )
        assert result["status"] == "inserted"


# ===========================================================================
# PROMPT 18 — implausibility BOUND (the gate), distinct from reference range
# ===========================================================================


class TestImplausibilityGate:
    """plausible_min/max is a GATE: an impossible value is blocked from prod and
    staged. It is checked BEFORE the confidence gate. A *reference-range* breach
    is NOT gated — it still writes and flags abnormal (rule #8)."""

    def test_implausibly_low_value_is_staged_not_written(self):
        """testosterone 2.75 (ng/mL slip of a ng/dL value) < plausible_min 50 →
        staged as IMPLAUSIBLE, at HIGH confidence — proving the gate fires ahead
        of the confidence gate, and is NOT a low-confidence stage."""
        conn = MagicMock()
        cur = MagicMock()
        cur.fetchone.side_effect = [
            (METRIC_UUID, "ng/dL"),                            # resolve exact (id, unit)
            (0.7,),                                            # confidence_min
            (1,),                                              # FK profile
            (1,),                                              # FK metric_def
            (None, None, "testosterone_total", 50.0, 2000.0),  # clinical none; plausible 50-2000
            (777,),                                            # stage INSERT … RETURNING id
        ]
        conn.cursor.return_value = cur

        result = ingest_record(
            conn, sync_log_id=18, kind="biomarker", table="biomarkers",
            raw={"profile_id": PROFILE_UUID, "metric_definition_id": METRIC_UUID,
                 "name": "testosterone_total", "measured_at": "2026-06-01 08:00+00",
                 "value": 2.75, "unit": "ng/dL"},
            conflict_cols=["profile_id", "metric_definition_id", "measured_at"],
        )
        # gate, not confidence: staged for the IMPLAUSIBLE reason at conf 1.0
        assert result["status"] == "staged"
        assert result["reason"] == "implausible_value"
        assert result["confidence"] == 1.0
        assert any(e["code"] == "implausible" for e in result.get("range_flags", []))
        # MUST route to staging, MUST NOT write to prod
        assert any("stg_biomarker_review" in str(c) for c in cur.execute.call_args_list)
        assert not any('INSERT INTO "biomarkers"' in str(c) for c in cur.execute.call_args_list)

    def test_plausible_value_writes(self):
        """testosterone 275 is inside plausible 50-2000 (no clinical range set) → writes."""
        conn = MagicMock()
        cur = MagicMock()
        cur.fetchone.side_effect = [
            (METRIC_UUID, "ng/dL"),                            # resolve exact
            (0.7,),                                            # confidence_min
            (1,),                                              # FK profile
            (1,),                                              # FK metric_def
            (None, None, "testosterone_total", 50.0, 2000.0),  # plausible 50-2000
            (True,),                                           # write RETURNING (xmax=0)
        ]
        conn.cursor.return_value = cur

        result = ingest_record(
            conn, sync_log_id=18, kind="biomarker", table="biomarkers",
            raw={"profile_id": PROFILE_UUID, "metric_definition_id": METRIC_UUID,
                 "name": "testosterone_total", "measured_at": "2026-06-01 08:00+00",
                 "value": 275, "unit": "ng/dL"},
            conflict_cols=["profile_id", "metric_definition_id", "measured_at"],
        )
        assert result["status"] == "inserted"
        assert not result.get("abnormal")

    def test_reference_breach_writes_and_flags_never_gated(self):
        """android_gynoid_ratio 1.27 > CLINICAL max 1.0 but inside plausible 0-5 →
        writes to prod + abnormal flag. Proves a reference breach is NOT gated."""
        conn = MagicMock()
        cur = MagicMock()
        cur.fetchone.side_effect = [
            (METRIC_UUID, "ratio"),                                # resolve exact
            (0.7,),                                                # confidence_min
            (1,),                                                  # FK profile
            (1,),                                                  # FK metric_def
            (None, 1.0, "android_gynoid_ratio", 0.0, 5.0),         # clinical max 1.0; plausible 0-5
            (True,),                                               # write RETURNING
        ]
        conn.cursor.return_value = cur

        result = ingest_record(
            conn, sync_log_id=18, kind="biomarker", table="biomarkers",
            raw={"profile_id": PROFILE_UUID, "metric_definition_id": METRIC_UUID,
                 "name": "android_gynoid_ratio", "measured_at": "2026-06-01 08:00+00",
                 "value": 1.27, "unit": "ratio"},
            conflict_cols=["profile_id", "metric_definition_id", "measured_at"],
        )
        assert result["status"] == "inserted"
        assert result.get("abnormal") is True
        assert any(e["code"] == "out_of_range" for e in result.get("range_flags", []))
        # NOT staged
        assert not any("stg_biomarker_review" in str(c) for c in cur.execute.call_args_list)

    def test_comma_slipped_food_calories_caught_by_bound(self):
        """A meal logged as 20 kcal (the '1,020' -> 20 comma-slip) < plausible_min
        25 → validate() flags it implausible (food path then stages it)."""
        conn = MagicMock()
        cur = MagicMock()
        cur.fetchone.side_effect = [
            (1,),                 # FK profile (food_logs FK = profiles)
            (25.0, 12000.0),      # food_energy_kcal plausible bounds
        ]
        conn.cursor.return_value = cur

        ok, errors = validate(conn, "food_logs", {
            "profile_id": PROFILE_UUID, "log_date": "2026-06-01", "calories": 20,
        })
        assert ok is False
        assert any(e["code"] == "implausible" for e in errors)

    def test_food_bound_missing_does_not_crash(self):
        """If food_energy_kcal is absent from the catalog, food logging must NOT
        fail — the plausibility check is simply skipped."""
        conn = MagicMock()
        cur = MagicMock()
        cur.fetchone.side_effect = [
            (1,),     # FK profile
            None,     # food_energy_kcal not found → skip the check
        ]
        conn.cursor.return_value = cur

        ok, errors = validate(conn, "food_logs", {
            "profile_id": PROFILE_UUID, "log_date": "2026-06-01", "calories": 20,
        })
        assert ok is True
        assert errors == []

    def test_heuristic_rejects_when_no_bound_set(self):
        """No plausible bound configured → 10x/0.1x heuristic vs prior history.
        value 5000 > 10x prior max 100 → implausible."""
        conn = MagicMock()
        cur = MagicMock()
        cur.fetchone.side_effect = [
            (1,),                                       # FK profile
            (1,),                                       # FK metric_def
            (None, None, "creatinine", None, None),     # NO plausible bounds
            (100.0, 50.0),                              # prior (max, min) for profile+metric
        ]
        conn.cursor.return_value = cur

        ok, errors = validate(conn, "biomarkers", {
            "profile_id": PROFILE_UUID, "metric_definition_id": METRIC_UUID,
            "measured_at": "2026-06-01 08:00+00", "value": 5000,
        })
        assert ok is False
        assert any(e["code"] == "implausible" and "10x prior" in e["message"]
                   for e in errors)

    def test_heuristic_accepts_when_no_prior_history(self):
        """No bound AND no prior data → cannot judge → accept (first reading)."""
        conn = MagicMock()
        cur = MagicMock()
        cur.fetchone.side_effect = [
            (1,),                                       # FK profile
            (1,),                                       # FK metric_def
            (None, None, "creatinine", None, None),     # no plausible bounds
            (None, None),                               # no prior history (empty max/min)
        ]
        conn.cursor.return_value = cur

        ok, errors = validate(conn, "biomarkers", {
            "profile_id": PROFILE_UUID, "metric_definition_id": METRIC_UUID,
            "measured_at": "2026-06-01 08:00+00", "value": 5000,
        })
        assert ok is True
        assert errors == []

    # --- unattended fail-safe: no bound AND no history -----------------------
    def _no_bound_no_history_cursor(self):
        cur = MagicMock()
        cur.fetchone.side_effect = [
            (1,),                                     # FK profile
            (1,),                                     # FK metric_def
            (None, None, "novel_marker", None, None),  # NO plausible bound
            (None, None),                             # NO prior history
        ]
        conn = MagicMock()
        conn.cursor.return_value = cur
        return conn

    _UNVERIFIABLE_ROW = {
        "profile_id": PROFILE_UUID, "metric_definition_id": METRIC_UUID,
        "measured_at": "2026-06-01 08:00+00", "value": 42,
    }

    def test_unattended_unverifiable_is_flagged_unverified(self):
        """UNATTENDED + no bound + no history → cannot verify → 'unverified' (stage)."""
        ok, errors = validate(self._no_bound_no_history_cursor(), "biomarkers",
                              dict(self._UNVERIFIABLE_ROW), attended=False)
        assert ok is False
        assert any(e["code"] == "unverified" for e in errors)

    def test_attended_unverifiable_is_accepted(self):
        """ATTENDED (a human confirms before write) accepts the same row."""
        ok, errors = validate(self._no_bound_no_history_cursor(), "biomarkers",
                              dict(self._UNVERIFIABLE_ROW), attended=True)
        assert ok is True
        assert errors == []

    def test_ingest_record_unattended_routes_unverifiable_to_staging(self):
        """End-to-end: an unattended run stages the unverifiable value (reason
        unverified_unattended) and does NOT write to prod."""
        conn = MagicMock()
        cur = MagicMock()
        cur.fetchone.side_effect = [
            (METRIC_UUID, "ng/mL"),                    # resolve exact
            (0.7,),                                    # confidence_min
            (1,),                                      # FK profile
            (1,),                                      # FK metric_def
            (None, None, "novel_marker", None, None),  # NO bound
            (None, None),                              # NO history
            (555,),                                    # stage INSERT … RETURNING id
        ]
        conn.cursor.return_value = cur

        result = ingest_record(
            conn, sync_log_id=18, kind="biomarker", table="biomarkers",
            raw={"profile_id": PROFILE_UUID, "metric_definition_id": METRIC_UUID,
                 "name": "novel_marker", "measured_at": "2026-06-01 08:00+00",
                 "value": 42, "unit": "ng/mL"},
            conflict_cols=["profile_id", "metric_definition_id", "measured_at"],
            attended=False,
        )
        assert result["status"] == "staged"
        assert result["reason"] == "unverified_unattended"
        assert any("stg_biomarker_review" in str(c) for c in cur.execute.call_args_list)
        assert not any('INSERT INTO "biomarkers"' in str(c) for c in cur.execute.call_args_list)

    def test_ingest_record_attended_writes_unverifiable(self):
        """The same value on an ATTENDED run writes to prod (default behaviour)."""
        conn = MagicMock()
        cur = MagicMock()
        cur.fetchone.side_effect = [
            (METRIC_UUID, "ng/mL"),                    # resolve exact
            (0.7,),                                    # confidence_min
            (1,),                                      # FK profile
            (1,),                                      # FK metric_def
            (None, None, "novel_marker", None, None),  # NO bound
            (None, None),                              # NO history → attended accepts
            (True,),                                   # write RETURNING (xmax=0)
        ]
        conn.cursor.return_value = cur

        result = ingest_record(
            conn, sync_log_id=18, kind="biomarker", table="biomarkers",
            raw={"profile_id": PROFILE_UUID, "metric_definition_id": METRIC_UUID,
                 "name": "novel_marker", "measured_at": "2026-06-01 08:00+00",
                 "value": 42, "unit": "ng/mL"},
            conflict_cols=["profile_id", "metric_definition_id", "measured_at"],
            attended=True,
        )
        assert result["status"] == "inserted"


# ===========================================================================
# open/close/log_error — sync log lifecycle
# ===========================================================================


class TestSyncLog:
    def test_open_returns_client_uuid_no_returning(self):
        # id is generated client-side (no RETURNING) so the maintainer-only SELECT
        # policy on wearable_sync_log can't break a non-maintainer's insert.
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value = cur

        sid = open_sync_log(conn, "whoop", "api", profile_id=PROFILE_UUID)
        uuid.UUID(sid)  # a valid UUID string
        sql, params = cur.execute.call_args[0]
        assert "wearable_sync_log" in sql
        assert "'in_progress'" in sql
        assert "RETURNING" not in sql.upper()
        assert sid in params  # the client id is passed in, not read back
        cur.fetchone.assert_not_called()

    def test_close_updates_status(self):
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value = cur

        close_sync_log(conn, 42, status="success",
                       records_in=10, records_upserted=8,
                       records_skipped=1, records_failed=1)
        sql = cur.execute.call_args[0][0]
        # routed through the SECURITY DEFINER finaliser (not a direct UPDATE), so a
        # non-maintainer can close its own run under the maintainer-only SELECT policy
        assert "hs_close_sync_log" in sql
        args = cur.execute.call_args[0][1]
        assert "success" in args

    def test_log_error_inserts_jsonb(self):
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value = cur

        raw_data = {"value": 999, "unit": "X"}
        log_error(conn, 42, "rec-1", "out_of_range", "value > max", raw_data)
        sql = cur.execute.call_args[0][0]
        assert "wearable_sync_errors" in sql
        assert "::jsonb" in sql
        args = cur.execute.call_args[0][1]
        # raw should be JSON-encoded dict, not the dict itself
        assert isinstance(args[4], str)
        assert json.loads(args[4]) == raw_data


# ===========================================================================
# db.resolve_profile() — no connection to live DB (mocked)
# ===========================================================================


class TestResolveProfile:
    """resolve_profile selects by UUID or display_name; never guesses."""

    def _conn(self, rows):
        conn = MagicMock()
        cur = MagicMock()
        cur.fetchall.return_value = rows
        cur.fetchone.return_value = rows[0] if rows else None
        conn.cursor.return_value = cur
        return conn

    def test_uuid_passthrough(self):
        from lib.db import resolve_profile
        uid = str(uuid.uuid4())
        conn = self._conn([(uid,)])
        assert resolve_profile(conn, uid) == uid

    def test_display_name_found(self):
        from lib.db import resolve_profile
        uid = str(uuid.uuid4())
        conn = MagicMock()
        cur = MagicMock()
        cur.fetchall.return_value = [(uid,)]
        conn.cursor.return_value = cur
        assert resolve_profile(conn, "PC") == uid

    def test_missing_raises(self):
        from lib.db import resolve_profile
        conn = MagicMock()
        cur = MagicMock()
        cur.fetchall.return_value = []
        conn.cursor.return_value = cur
        with pytest.raises(LookupError, match="no profile"):
            resolve_profile(conn, "NoSuchPerson")

    def test_ambiguous_raises(self):
        from lib.db import resolve_profile
        uid1, uid2 = str(uuid.uuid4()), str(uuid.uuid4())
        conn = MagicMock()
        cur = MagicMock()
        cur.fetchall.return_value = [(uid1,), (uid2,)]
        conn.cursor.return_value = cur
        with pytest.raises(LookupError, match="ambiguous"):
            resolve_profile(conn, "PC")

    def test_defaults_to_pc(self):
        from lib.db import resolve_profile
        uid = str(uuid.uuid4())
        conn = MagicMock()
        cur = MagicMock()
        cur.fetchall.return_value = [(uid,)]
        conn.cursor.return_value = cur
        result = resolve_profile(conn)  # no `who` arg
        assert result == uid
        # must have queried for 'PC'
        sql_args = cur.execute.call_args[0][1]
        assert "PC" in str(sql_args)


# ===========================================================================
# Prevention rule #1 — parse_number (root-cause fix for "1,020 kcal" → 20)
# ===========================================================================

from lib.contract import parse_number


class TestParseNumber:
    def test_thousands_separator_preserved(self):
        # the exact audit-bug inputs
        assert parse_number("1,020 kcal") == (1020.0, None)
        assert parse_number("~1,050 kcal") == (1050.0, None)
        assert parse_number("2,997 g") == (2997.0, None)

    def test_plain_numbers(self):
        assert parse_number(5.2) == (5.2, None)
        assert parse_number("5.2") == (5.2, None)
        assert parse_number("112 mg/dL") == (112.0, None)

    def test_below_detection_qualifier(self):
        assert parse_number("<10.00 mg/dL") == (10.0, "<")
        assert parse_number(">1000") == (1000.0, ">")
        assert parse_number("<=5") == (5.0, "<=")

    def test_no_number(self):
        assert parse_number("Non Reactive") == (None, None)
        assert parse_number(None) == (None, None)

    def test_decimal_only(self):
        assert parse_number(".59") == (0.59, None)
        assert parse_number("0.11 S/CO") == (0.11, None)


# ===========================================================================
# Integration — GENERATED-column introspection (live DB; skips when unreachable)
# ===========================================================================
# Regression guard for the supplement_intake_logs.taken_on 428C9 bug: write()
# must NEVER put a GENERATED column in the INSERT list. _generated_cols() learns
# that from pg_attribute. On a locked-down role with no pg_attribute read, the
# except branch degrades to "none generated" and the bug silently reopens — this
# test fails LOUDLY if taken_on stops being detected.
class TestGeneratedColsLive:
    def _conn(self):
        import os, sys
        here = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        sys.path.insert(0, os.path.join(here, "scripts"))
        try:
            import hs_ops
            hs_ops._load_env()
            return hs_ops.connect(readonly=True)
        except Exception as e:
            pytest.skip(f"no live DB for generated-col introspection: {e}")

    def test_taken_on_detected_as_generated(self):
        from lib.contract import _generated_cols
        conn = self._conn()
        try:
            gen = _generated_cols(conn, "supplement_intake_logs")
        finally:
            conn.close()
        assert "taken_on" in gen, (
            "supplement_intake_logs.taken_on no longer detected as GENERATED — "
            "write() would try to INSERT it and raise 428C9 (check pg_attribute "
            "read access on the connecting role)"
        )
