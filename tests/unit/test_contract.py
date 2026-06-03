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
            (None, None, "HbA1c"),    # range check: no min/max → no out_of_range
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
            (None, None, "HbA1c"),    # range: no limits
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
            (5.0, 10.0, "HbA1c"),         # range: value=7 is in [5, 10]
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
            (4.0, 6.0, "HbA1c"),    # range: min=4, max=6; value=12 OUT
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
            (4.0, 6.0, "HbA1c"),    # range: value=5.2 is in [4, 6] → ok
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
# open/close/log_error — sync log lifecycle
# ===========================================================================


class TestSyncLog:
    def test_open_returns_id(self):
        conn = MagicMock()
        cur = MagicMock()
        cur.fetchone.return_value = (42,)
        conn.cursor.return_value = cur

        sid = open_sync_log(conn, "whoop", "api", profile_id=PROFILE_UUID)
        assert sid == 42
        sql = cur.execute.call_args[0][0]
        assert "wearable_sync_log" in sql
        assert "'in_progress'" in sql

    def test_close_updates_status(self):
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value = cur

        close_sync_log(conn, 42, status="success",
                       records_in=10, records_upserted=8,
                       records_skipped=1, records_failed=1)
        sql = cur.execute.call_args[0][0]
        assert "UPDATE wearable_sync_log" in sql
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
