"""Unit tests for analysis/trends.py, supplement_summary.py, food_chat.py (V3-5). DB mocked."""
from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch

from analysis import trends, supplement_summary, food_chat

PROFILE = str(uuid.uuid4())


class TestSleepTrend:
    def test_null_recovery_reported_separately(self):
        rec = {"summary": {"scored_days": 23, "no_sleep_days": 4,
                           "avg_recovery": 67.7, "avg_hrv": 60.0, "avg_rhr": 64.0}}
        debt = {"rows": [
            {"sleep_performance_pct": 80, "asleep_duration_min": 420, "sleep_debt_min": 30},
            {"sleep_performance_pct": 84, "asleep_duration_min": 450, "sleep_debt_min": 10},
            {"sleep_performance_pct": 78, "asleep_duration_min": 400, "sleep_debt_min": 45},
            {"sleep_performance_pct": 88, "asleep_duration_min": 470, "sleep_debt_min": 5},
        ]}
        with patch.object(trends, "run_view", side_effect=[rec, debt]):
            s = trends.sleep_trend(MagicMock(), PROFILE)
        assert s["scored_nights"] == 23 and s["no_sleep_nights"] == 4
        assert s["avg_sleep_performance_pct"] == 82.5   # mean of the 4 scored rows
        assert "no sleep score" in s["narrative"]       # NULLs surfaced, not averaged in


class TestWorkoutTrend:
    def test_intensity_volume(self):
        rows = {"rows": [
            {"duration_min": 60, "hi_intensity_min": 12, "z2_min": 30},
            {"duration_min": 45, "hi_intensity_min": 0, "z2_min": 40},
        ]}
        with patch.object(trends, "run_view", return_value=rows):
            w = trends.workout_trend(MagicMock(), PROFILE, days=14)
        assert w["workouts"] == 2 and w["hi_intensity_min"] == 12.0 and w["zone2_min"] == 70.0
        assert w["hi_intensity_min_per_week"] == 6.0     # 12 over 2 weeks

    def test_no_workouts(self):
        with patch.object(trends, "run_view", return_value={"rows": []}):
            w = trends.workout_trend(MagicMock(), PROFILE)
        assert w["workouts"] == 0 and "No workouts" in w["narrative"]


class TestSupplementSummary:
    def test_adherence_daily_vs_cyclical(self):
        conn = MagicMock()
        regcur = MagicMock()
        regcur.fetchall.return_value = [
            {"id": "r1", "supplement_id": "s1", "supplement": "Creatine",
             "dose_amount": 5, "dose_unit": "g", "timing": "morning",
             "frequency": "daily", "status": "active", "cycle_days_on": None,
             "cycle_days_off": None, "purpose": "performance"},
            {"id": "r2", "supplement_id": "s2", "supplement": "Berberine",
             "dose_amount": 500, "dose_unit": "mg", "timing": "with meals",
             "frequency": "daily", "status": "active", "cycle_days_on": 5,
             "cycle_days_off": 2, "purpose": "glucose"},
        ]
        adhcur = MagicMock()
        adhcur.fetchall.return_value = [("s1", 7), ("s2", 10)]
        conn.cursor.side_effect = [regcur, adhcur]
        out = supplement_summary.summary(conn, PROFILE, days=14)
        by = {i["supplement"]: i for i in out["regimens"]}
        assert by["Creatine"]["adherence_pct"] == 50.0   # 7/14 daily
        assert by["Berberine"]["adherence_pct"] is None    # cyclical → pct not meaningful
        assert out["low_adherence_count"] == 1             # creatine < 70%


class TestFoodChat:
    def test_preview_does_not_write(self):
        conn = MagicMock()
        with patch.object(food_chat, "_lookup_guidance",
                          return_value=([{"item": "eggs", "classification": "green"}], [])), \
             patch.object(food_chat, "classify_food",
                          return_value={"calories": 180, "protein_g": 14, "verdict": "green",
                                        "flags": ["high_protein"], "confidence": 0.9}):
            p = food_chat.preview_meal(conn, PROFILE, items=["eggs"])
        assert p["needs_confirmation"] is True
        assert p["parsed"]["calories"] == 180
        # preview must NOT have executed any INSERT
        assert not any("INSERT" in str(c) for c in conn.cursor().execute.call_args_list)

    def test_log_meal_delegates_to_contract(self):
        with patch.object(food_chat, "ingest_food_row",
                          return_value={"status": "inserted", "confidence": 0.9}) as m:
            r = food_chat.log_meal(MagicMock(), PROFILE, items=["eggs"], method="photo")
        assert r["status"] == "inserted"
        assert m.call_args.kwargs["method"] == "photo"

    def test_daily_total_vs_target(self):
        conn = MagicMock()
        cur = MagicMock()
        cur.fetchone.return_value = (1600, 120, 3)   # cal, protein, meals
        conn.cursor.return_value = cur
        out = food_chat.daily_total(conn, PROFILE, calorie_target=2000)
        assert out["consumed_cal"] == 1600 and out["target_cal"] == 2000
        assert out["remaining_cal"] == 400 and out["pct_of_target"] == 80

    def test_daily_total_no_target_stays_none(self):
        conn = MagicMock()
        cur = MagicMock()
        cur.fetchone.return_value = (1600, 120, 3)
        conn.cursor.return_value = cur
        out = food_chat.daily_total(conn, PROFILE)   # no target → skill must ask, not assume
        assert out["target_cal"] is None and out["pct_of_target"] is None
