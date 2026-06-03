"""Unit tests for monitor/trend_monitor.py + monitor/ingest_health.py (V3-4). DB mocked."""
from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch

from monitor import trend_monitor as tm
from monitor import ingest_health as ih

PROFILE = str(uuid.uuid4())


def _dictcur(fetchall=None, fetchone=None):
    cur = MagicMock()
    cur.fetchall.return_value = fetchall or []
    cur.fetchone.return_value = fetchone
    return cur


class TestRecoveryHrvShifts:
    def test_flags_drop_beyond_prior_stddev(self):
        conn = MagicMock()
        conn.cursor.return_value = _dictcur(fetchall=[
            {"win": "recent", "ar": 50.0, "sr": 5.0, "n": 6, "ah": 45.0, "sh": 5.0},
            {"win": "prior",  "ar": 70.0, "sr": 6.0, "n": 20, "ah": 65.0, "sh": 8.0},
        ])
        out = tm.recovery_hrv_shifts(conn, PROFILE)
        # recovery dropped 20 (> prior sd 6) and HRV dropped 20 (> prior sd 8) → 2 findings
        assert len(out) == 2
        assert all(f["alert_type"] == "trend" and f["severity"] == "warning" for f in out)

    def test_no_flag_within_noise(self):
        conn = MagicMock()
        conn.cursor.return_value = _dictcur(fetchall=[
            {"win": "recent", "ar": 68.0, "sr": 5.0, "n": 6, "ah": 64.0, "sh": 5.0},
            {"win": "prior",  "ar": 70.0, "sr": 6.0, "n": 20, "ah": 65.0, "sh": 8.0},
        ])
        # drops of 2 and 1 are within the prior sd → no finding
        assert tm.recovery_hrv_shifts(conn, PROFILE) == []

    def test_insufficient_history_no_flag(self):
        conn = MagicMock()
        conn.cursor.return_value = _dictcur(fetchall=[
            {"win": "recent", "ar": 50.0, "sr": 5.0, "n": 1, "ah": 45.0, "sh": 5.0},
            {"win": "prior",  "ar": 70.0, "sr": 6.0, "n": 2, "ah": 65.0, "sh": 8.0},
        ])
        assert tm.recovery_hrv_shifts(conn, PROFILE) == []


class TestStrapCompliance:
    def test_flags_when_over_third_missing(self):
        conn = MagicMock()
        conn.cursor.return_value = _dictcur(fetchone=(12, 5))  # 5/12 = 42% > 1/3
        out = tm.strap_compliance(conn, PROFILE)
        assert len(out) == 1 and out[0]["alert_type"] == "anomaly"

    def test_no_flag_when_compliant(self):
        conn = MagicMock()
        conn.cursor.return_value = _dictcur(fetchone=(14, 2))  # 2/14 = 14%
        assert tm.strap_compliance(conn, PROFILE) == []


class TestCheckAggregates:
    def test_check_sorts_by_severity_and_swallows_monitor_errors(self):
        conn = MagicMock()
        with patch.object(tm, "recovery_hrv_shifts", return_value=[
                 tm._finding("trend", "warning", "HRV down", "m")]), \
             patch.object(tm, "strap_compliance", side_effect=RuntimeError("boom")), \
             patch.object(tm, "abnormal_biomarkers", return_value=[
                 tm._finding("threshold", "critical", "ApoB HIGH", "m")]), \
             patch.object(tm, "goal_findings", return_value=[
                 tm._finding("goal", "info", "Goal behind", "m")]):
            out = tm.check(conn, PROFILE, persist=False)
        # critical first, info last; the failing monitor didn't crash the run
        assert [f["severity"] for f in out] == ["critical", "warning", "info"]


class TestIngestHealthMaintainerGate:
    def test_non_maintainer_short_circuits(self):
        conn = MagicMock()
        cur = MagicMock()
        cur.fetchone.return_value = (False,)   # is_maintainer() = false
        conn.cursor.return_value = cur
        out = ih.check(conn)
        assert out == {"maintainer": False, "items": []}

    def test_maintainer_surfaces_staging_and_failures(self):
        conn = MagicMock()
        gate = MagicMock(); gate.fetchone.return_value = (True,)         # is_maintainer()
        dc = MagicMock()
        # staging biomarker=3, food=0; then failed runs; then errors
        dc.fetchone.side_effect = [
            {"n": 3},                                   # stg_biomarker_review pending
            {"n": 0},                                   # stg_food_log_review pending
            {"runs": 2, "recs": 4},                     # failed runs
        ]
        dc.fetchall.return_value = [{"error_code": "implausible", "n": 4}]
        conn.cursor.side_effect = [gate, dc]
        out = ih.check(conn)
        assert out["maintainer"] is True
        kinds = {i["kind"] for i in out["items"]}
        assert "staging" in kinds and "sync_failure" in kinds and "errors" in kinds
        staging = [i for i in out["items"] if i["kind"] == "staging"]
        assert len(staging) == 1 and staging[0]["count"] == 3   # only the non-zero queue
