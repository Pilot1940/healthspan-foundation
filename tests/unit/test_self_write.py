"""Unit tests for ingest/self_write.py — the non-maintainer / strength direct-write path.

Invariants under test:
  1. Routes through DbRest.insert() (the App path) and never sends a GENERATED column.
  2. Works over a psycopg2 connection too (local/PC), committing and returning the id.
  3. Each kind sets profile_id + source and drops None fields.
All handles are mocked — no live DB, no network.
"""
from __future__ import annotations

import uuid
from unittest.mock import MagicMock

from ingest import self_write

PROFILE = str(uuid.uuid4())
SUPP = str(uuid.uuid4())
METRIC = str(uuid.uuid4())


class _FakeDbRest:
    """Minimal DbRest stand-in: has .insert and ._base (the duck-typed markers)."""
    _base = "https://test.supabase.co"

    def __init__(self):
        self.calls = []

    def insert(self, table, row, **_):
        self.calls.append((table, row))
        return {"id": "new-id"}


def test_strength_routes_through_dbrest_and_omits_generated():
    db = _FakeDbRest()
    rid = self_write.log_strength(
        db, PROFILE, performed_at="2026-06-11T13:30:00+07:00",
        exercise="trap_bar_deadlift", modality="barbell", load_value=165,
        load_unit="lb", sets=5, reps=5, device_specific=False,
    )
    assert rid == "new-id"
    table, row = db.calls[0]
    assert table == "strength_logs"
    assert row["profile_id"] == PROFILE
    assert row["exercise"] == "trap_bar_deadlift"
    assert row["source"] == "skill"
    # performed_on is GENERATED — must never be sent.
    assert "performed_on" not in row
    # None fields dropped (rir/notes not supplied).
    assert "rir" not in row and "notes" not in row


def test_food_self_write_sets_profile_and_drops_none():
    db = _FakeDbRest()
    self_write.log_food(db, PROFILE, description="2 eggs", meal_type="breakfast",
                        calories=140, protein_g=12)
    table, row = db.calls[0]
    assert table == "food_logs"
    assert row["profile_id"] == PROFILE and row["meal_type"] == "breakfast"
    assert "log_date" not in row          # GENERATED
    assert "carbs_g" not in row           # None dropped


def test_supplement_self_write_shape():
    db = _FakeDbRest()
    self_write.log_supplement(db, PROFILE, supplement_id=SUPP,
                             taken_at="2026-06-11T08:00:00Z", dose_amount=1000,
                             dose_unit="mg")
    table, row = db.calls[0]
    assert table == "supplement_intake_logs"
    assert row["supplement_id"] == SUPP and row["source"] == "skill"
    assert "taken_on" not in row          # GENERATED


def test_biomarker_self_write_shape():
    db = _FakeDbRest()
    self_write.log_biomarker(db, PROFILE, metric_definition_id=METRIC, value=5.4,
                            measured_at="2026-06-05T00:00:00Z", unit="%")
    table, row = db.calls[0]
    assert table == "biomarkers"
    assert row["metric_definition_id"] == METRIC and row["value"] == 5.4


def test_psycopg2_path_inserts_and_commits():
    """Over a psycopg2 connection: parameterised INSERT … RETURNING id, then commit."""
    cur = MagicMock()
    cur.fetchone.return_value = ("pg-id",)
    conn = MagicMock()
    conn.cursor.return_value = cur
    # ensure it is NOT treated as a DbRest
    del conn._base

    rid = self_write.log_strength(conn, PROFILE, performed_at="2026-06-11T13:30:00+07:00",
                                  exercise="machine_chest_press", modality="machine",
                                  load_value=140, load_unit="lb", device_specific=True)
    assert rid == "pg-id"
    sql = cur.execute.call_args[0][0]
    assert "INSERT INTO public.strength_logs" in sql and "RETURNING id" in sql
    assert "performed_on" not in sql      # GENERATED never named
    conn.commit.assert_called_once()
