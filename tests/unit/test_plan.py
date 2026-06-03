"""Unit tests for plan/goals.py + plan/training_plan.py (V3-3). DB mocked."""
from __future__ import annotations

import uuid
from unittest.mock import MagicMock

import pytest

from plan.goals import compute_progress, create_goal, track_goals
from plan.training_plan import create_training_plan

PROFILE = str(uuid.uuid4())
METRIC = str(uuid.uuid4())


class TestComputeProgress:
    def test_raising_goal(self):
        # VO2max 40 -> 50, currently 45 = halfway
        assert compute_progress(45, 40, 50) == 50.0

    def test_lowering_goal(self):
        # ApoB 100 -> 70, currently 85 = halfway (direction-aware)
        assert compute_progress(85, 100, 70) == 50.0

    def test_clamped_past_target(self):
        assert compute_progress(55, 40, 50) == 100.0   # overshoot raise
        assert compute_progress(60, 100, 70) == 100.0  # overshoot lower

    def test_clamped_below_baseline(self):
        assert compute_progress(38, 40, 50) == 0.0

    def test_none_when_missing_or_degenerate(self):
        assert compute_progress(None, 40, 50) is None
        assert compute_progress(45, None, 50) is None
        assert compute_progress(45, 40, None) is None
        assert compute_progress(45, 50, 50) is None    # target == baseline


class TestCreateGoal:
    def _conn(self, metric_row):
        cur = MagicMock()
        # first fetchone = _resolve_metric lookup
        cur.fetchone.return_value = metric_row
        conn = MagicMock()
        conn.cursor.return_value = cur
        return conn, cur

    def test_resolves_metric_and_inserts(self):
        conn, cur = self._conn((METRIC, "ng/dL"))
        g = create_goal(conn, PROFILE, "Raise testosterone",
                        metric_name="testosterone_total", target_value=700,
                        baseline_value=400)
        assert g["metric_definition_id"] == METRIC
        assert g["target_unit"] == "ng/dL"          # defaulted from catalog
        insert = [c for c in cur.execute.call_args_list if "INSERT INTO user_goals" in str(c)]
        assert len(insert) == 1

    def test_unresolved_metric_left_null(self):
        conn, cur = self._conn(None)  # no catalog match
        g = create_goal(conn, PROFILE, "Feel more rested", metric_name="vibes")
        assert g["metric_definition_id"] is None     # never invented

    def test_multiple_concurrent_goals_not_blocked(self):
        # create_goal imposes no client-side one-active limit (020 dropped the DB uniq)
        conn, cur = self._conn((METRIC, "ml/kg/min"))
        for i in range(3):
            cur.fetchone.return_value = (METRIC, "ml/kg/min")
            create_goal(conn, PROFILE, f"goal {i}", metric_name="vo2max_estimated")
        inserts = [c for c in cur.execute.call_args_list if "INSERT INTO user_goals" in str(c)]
        assert len(inserts) == 3


class TestTrackGoals:
    def test_progress_from_latest_reading(self):
        import psycopg2.extras  # noqa
        conn = MagicMock()
        # list_goals uses a RealDict cursor; _latest_metric_value uses a plain cursor.
        dict_cur = MagicMock()
        dict_cur.fetchall.return_value = [{
            "id": "g1", "title": "Lower ApoB", "description": None,
            "metric_definition_id": METRIC, "metric_name": "apo_b",
            "target_value": 70, "target_unit": "mg/dL", "target_date": None,
            "baseline_value": 100, "program_id": None, "is_active": True,
            "progress_pct": 0, "created_at": None,
        }]
        plain_cur = MagicMock()
        plain_cur.fetchone.return_value = (85,)   # latest apo_b reading
        conn.cursor.side_effect = [dict_cur, plain_cur]

        tracked = track_goals(conn, PROFILE)
        assert len(tracked) == 1
        assert tracked[0]["current_value"] == 85
        assert tracked[0]["progress_pct"] == 50.0   # 100->70, at 85
        assert tracked[0]["status"] == "on_track"


class TestCreateTrainingPlan:
    def test_program_plus_phases_inserted(self):
        cur = MagicMock()
        conn = MagicMock()
        conn.cursor.return_value = cur
        plan = create_training_plan(
            conn, PROFILE, "Marathon base", objective="sub-3:30",
            target_event="Berlin 2026",
            phases=[{"name": "Base", "weekly_template": {"mon": "Z2 60min"}},
                    {"name": "Build", "weekly_template": {"wed": "VO2 4x4"}}],
        )
        assert len(plan["phases"]) == 2
        assert plan["phases"][0]["ordinal"] == 0 and plan["phases"][1]["ordinal"] == 1
        prog_ins = [c for c in cur.execute.call_args_list if "INSERT INTO training_programs" in str(c)]
        phase_ins = [c for c in cur.execute.call_args_list if "INSERT INTO program_phases" in str(c)]
        assert len(prog_ins) == 1 and len(phase_ins) == 2

    def test_bad_status_rejected(self):
        with pytest.raises(ValueError, match="status"):
            create_training_plan(MagicMock(), PROFILE, "x", status="bogus")
