"""Unit tests for lib/safe_ops.py — date-safe correction utilities.

All DB calls mocked — no live DB. Proves:
  1. today_utc() returns a valid YYYY-MM-DD string
  2. safe_bulk_update enforces non-empty id list and dry-run default
  3. reassign_day_by_ids enforces non-empty id list, table whitelist, and
     preserves the original timestamp in notes for recovery
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock, call

import pytest

from lib.safe_ops import reassign_day_by_ids, safe_bulk_update, today_utc


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_cursor(fetchall_rows=None, description=None):
    cur = MagicMock()
    cur.__enter__ = lambda s: s
    cur.__exit__ = MagicMock(return_value=False)
    cur.fetchall.return_value = fetchall_rows or []
    cur.description = description or []
    return cur


def _conn_for_select(rows, cols):
    """Mock conn whose cursor() returns a SELECT cursor with given rows/cols."""
    desc = [(c,) for c in cols]
    cur = _make_cursor(fetchall_rows=rows, description=desc)
    conn = MagicMock()
    conn.cursor.return_value = cur
    return conn


# ── today_utc ─────────────────────────────────────────────────────────────────

def test_today_utc_format():
    result = today_utc()
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", result), f"Expected YYYY-MM-DD, got: {result!r}"


def test_today_utc_is_string():
    assert isinstance(today_utc(), str)


# ── safe_bulk_update — guards ─────────────────────────────────────────────────

def test_safe_bulk_update_empty_ids_raises():
    with pytest.raises(ValueError, match="non-empty"):
        safe_bulk_update(MagicMock(), "food_logs", ids=[], update_data={"notes": "x"})


def test_safe_bulk_update_empty_update_data_raises():
    with pytest.raises(ValueError):
        safe_bulk_update(MagicMock(), "food_logs", ids=["id1"], update_data={})


# ── safe_bulk_update — dry_run (default) ─────────────────────────────────────

def test_safe_bulk_update_dry_run_does_not_call_update():
    row_id = str(uuid.uuid4())
    conn = _conn_for_select([(row_id, "lunch", 500)], ["id", "meal_type", "calories"])

    result = safe_bulk_update(conn, "food_logs", ids=[row_id], update_data={"calories": 450})

    # UPDATE must never be called in dry_run mode
    executed_sql = [str(c) for c in conn.cursor.return_value.execute.call_args_list]
    assert not any("UPDATE" in s for s in executed_sql), "UPDATE must not run in dry_run=True"
    assert not conn.commit.called
    assert result[0]["_action"] == "would_update"
    assert result[0]["_pending"] == {"calories": 450}


# ── safe_bulk_update — mutating ───────────────────────────────────────────────

def test_safe_bulk_update_dry_run_false_calls_update_and_commit():
    row_id = str(uuid.uuid4())

    select_rows = [(row_id, "lunch", 500)]
    select_desc = ["id", "meal_type", "calories"]

    # First call: SELECT (fetch current); second: UPDATE; third: SELECT (fetch updated)
    cur_select1 = _make_cursor(select_rows, [(c,) for c in select_desc])
    cur_update  = _make_cursor()
    cur_select2 = _make_cursor([(row_id, "lunch", 450)], [(c,) for c in select_desc])

    call_count = 0

    def _cursor():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return cur_select1
        if call_count == 2:
            return cur_update
        return cur_select2

    conn = MagicMock()
    conn.cursor.side_effect = _cursor

    result = safe_bulk_update(
        conn, "food_logs",
        ids=[row_id], update_data={"calories": 450},
        dry_run=False,
    )

    assert conn.commit.called
    assert result[0]["_action"] == "updated"
    assert result[0]["calories"] == 450


# ── reassign_day_by_ids — guards ──────────────────────────────────────────────

def test_reassign_empty_ids_raises():
    with pytest.raises(ValueError, match="non-empty"):
        reassign_day_by_ids(MagicMock(), "food_logs", ids=[], new_taken_at="2026-06-07T08:00:00+00:00")


def test_reassign_unsupported_table_raises():
    with pytest.raises(ValueError, match="not supported"):
        reassign_day_by_ids(MagicMock(), "biomarkers", ids=["id1"], new_taken_at="2026-06-07")


# ── reassign_day_by_ids — dry_run (default) ───────────────────────────────────

def test_reassign_dry_run_food_logs_shows_preview_with_original():
    row_id = str(uuid.uuid4())
    original_ts = datetime(2026, 6, 5, 8, 30, tzinfo=timezone.utc)

    cur = _make_cursor(
        fetchall_rows=[(row_id, original_ts, "post-workout")],
        description=[("id",), ("logged_at",), ("notes",)],
    )
    conn = MagicMock()
    conn.cursor.return_value = cur

    result = reassign_day_by_ids(
        conn, "food_logs",
        ids=[row_id],
        new_taken_at="2026-06-07T08:30:00+00:00",
    )

    assert len(result) == 1
    r = result[0]
    assert r["action"] == "would_reassign"
    assert "2026-06-05" in r["old_logged_at"] or "2026-06-05" in str(r["old_logged_at"])
    assert r["new_logged_at"] == "2026-06-07T08:30:00+00:00"
    assert "prior logged_at" in r["notes_preview"]
    assert "post-workout" in r["notes_preview"]
    # No mutation
    assert not conn.commit.called


def test_reassign_dry_run_does_not_mutate():
    row_id = str(uuid.uuid4())
    cur = _make_cursor(
        fetchall_rows=[(row_id, datetime(2026, 6, 5, 8, 0, tzinfo=timezone.utc), None)],
        description=[("id",), ("taken_at",), ("notes",)],
    )
    conn = MagicMock()
    conn.cursor.return_value = cur

    reassign_day_by_ids(
        conn, "supplement_intake_logs",
        ids=[row_id], new_taken_at="2026-06-07T08:00:00+00:00",
    )

    executed = [str(c) for c in cur.execute.call_args_list]
    assert not any("UPDATE" in s for s in executed)


# ── reassign_day_by_ids — food_logs (mutating) ───────────────────────────────

def test_reassign_food_logs_updates_logged_at_and_log_date():
    row_id = str(uuid.uuid4())
    original_ts = datetime(2026, 6, 5, 8, 30, tzinfo=timezone.utc)

    cur_select = _make_cursor(
        fetchall_rows=[(row_id, original_ts, None)],
        description=[("id",), ("logged_at",), ("notes",)],
    )
    cur_update = _make_cursor()

    call_count = 0

    def _cursor():
        nonlocal call_count
        call_count += 1
        return cur_select if call_count == 1 else cur_update

    conn = MagicMock()
    conn.cursor.side_effect = _cursor

    result = reassign_day_by_ids(
        conn, "food_logs",
        ids=[row_id], new_taken_at="2026-06-07T09:00:00+00:00",
        dry_run=False,
    )

    assert conn.commit.called
    assert result[0]["action"] == "reassigned"
    assert result[0]["new_date"] == "2026-06-07"

    # Verify the UPDATE included both logged_at and log_date
    update_sql = str(cur_update.execute.call_args)
    assert "log_date" in update_sql
    assert "logged_at" in update_sql


# ── reassign_day_by_ids — supplement (mutating, taken_on generated) ──────────

def test_reassign_supplement_does_not_write_taken_on():
    row_id = str(uuid.uuid4())
    original_ts = datetime(2026, 6, 5, 7, 0, tzinfo=timezone.utc)

    cur_select = _make_cursor(
        fetchall_rows=[(row_id, original_ts, None)],
        description=[("id",), ("taken_at",), ("notes",)],
    )
    cur_update = _make_cursor()

    call_count = 0

    def _cursor():
        nonlocal call_count
        call_count += 1
        return cur_select if call_count == 1 else cur_update

    conn = MagicMock()
    conn.cursor.side_effect = _cursor

    result = reassign_day_by_ids(
        conn, "supplement_intake_logs",
        ids=[row_id], new_taken_at="2026-06-07T07:00:00+00:00",
        dry_run=False,
    )

    update_sql = str(cur_update.execute.call_args)
    assert "taken_at" in update_sql
    assert "taken_on" not in update_sql  # GENERATED — must never be written
    assert conn.commit.called


def test_reassign_preserves_original_ts_in_notes():
    row_id = str(uuid.uuid4())
    original_ts = datetime(2026, 6, 5, 8, 30, tzinfo=timezone.utc)

    cur_select = _make_cursor(
        fetchall_rows=[(row_id, original_ts, "morning stack")],
        description=[("id",), ("taken_at",), ("notes",)],
    )
    cur_update = _make_cursor()

    call_count = 0

    def _cursor():
        nonlocal call_count
        call_count += 1
        return cur_select if call_count == 1 else cur_update

    conn = MagicMock()
    conn.cursor.side_effect = _cursor

    reassign_day_by_ids(
        conn, "supplement_intake_logs",
        ids=[row_id], new_taken_at="2026-06-07T07:00:00+00:00",
        dry_run=False,
    )

    update_call = cur_update.execute.call_args
    # Third positional param in (taken_at, notes, id) is notes
    params = update_call[0][1]  # positional args to execute: (sql, params_tuple)
    notes_value = params[1]  # (dt, new_notes, row_id)
    assert "prior taken_at" in notes_value
    assert "morning stack" in notes_value
