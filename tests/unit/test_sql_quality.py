"""Unit tests for the V3-6 sql_guard quality layer + audit."""
from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch

from lib.sql_guard import quality_check, audit_query, run_adhoc_audited

PROFILE = str(uuid.uuid4())


class TestQualityCheck:
    def test_fanout_join_without_key_flagged(self):
        sql = ("SELECT count(*) FROM whoop_cycles c "
               "JOIN whoop_workouts w "          # no ON → fan-out
               "WHERE c.profile_id = %(profile_id)s")
        v = quality_check(sql, rows=[{"count": 999}])
        assert v["ok"] is False
        assert any(f["code"] == "join_without_key" for f in v["flags"])

    def test_comma_join_flagged(self):
        v = quality_check("SELECT * FROM whoop_cycles c, whoop_workouts w", rows=[{"x": 1}])
        assert any(f["code"] == "comma_join" for f in v["flags"]) and v["ok"] is False

    def test_aggregate_over_nullable_flagged(self):
        sql = ("SELECT avg(recovery_score_pct) FROM whoop_cycles "
               "WHERE profile_id=%(profile_id)s AND cycle_start >= current_date - interval '30 days'")
        v = quality_check(sql, rows=[{"avg": 50}])
        assert any(f["code"] == "aggregate_over_nullable" for f in v["flags"])

    def test_aggregate_with_filter_is_clean(self):
        sql = ("SELECT avg(recovery_score_pct) FILTER (WHERE recovery_score_pct IS NOT NULL) "
               "FROM whoop_cycles WHERE profile_id=%(profile_id)s "
               "AND cycle_start >= current_date - interval '30 days'")
        v = quality_check(sql, rows=[{"avg": 67.7}])
        assert v["ok"] is True
        assert not any(f["code"] == "aggregate_over_nullable" for f in v["flags"])

    def test_clean_scoped_query_passes(self):
        sql = ("SELECT cycle_start::date, recovery_score_pct FROM whoop_cycles "
               "WHERE profile_id=%(profile_id)s AND cycle_start >= current_date - interval '7 days'")
        v = quality_check(sql, rows=[{"recovery_score_pct": 70}])
        assert v["ok"] is True and v["notes"] == "clean"

    def test_empty_result_is_high_flag(self):
        sql = "SELECT * FROM whoop_cycles WHERE profile_id=%(profile_id)s AND cycle_start::date = '1990-01-01'"
        v = quality_check(sql, rows=[])
        assert v["ok"] is False and any(f["code"] == "empty_result" for f in v["flags"])

    def test_missing_time_window_is_low_advisory_only(self):
        # aggregate over a time-series table with no date filter → low flag, still ok
        sql = "SELECT avg(day_strain) FROM whoop_cycles WHERE profile_id=%(profile_id)s"
        v = quality_check(sql, rows=[{"avg": 10}])
        assert v["ok"] is True  # only a low advisory, not a high flag
        assert any(f["code"] == "aggregate_no_time_window" and f["severity"] == "low"
                   for f in v["flags"])


class TestAuditQuery:
    def test_audit_inserts_no_returning(self):
        conn = MagicMock(); cur = MagicMock(); conn.cursor.return_value = cur
        audit_query(conn, PROFILE, intent="recovery trend",
                    sql="SELECT 1", plan=None,
                    verdict={"ok": True, "flags": [], "notes": "clean"}, row_count=5)
        sql = cur.execute.call_args[0][0]
        assert "INSERT INTO query_audit" in sql and "RETURNING" not in sql.upper()
        params = cur.execute.call_args[0][1]
        assert params[-1] is False   # flagged = not ok

    def test_flagged_true_when_high_flag(self):
        conn = MagicMock(); cur = MagicMock(); conn.cursor.return_value = cur
        audit_query(conn, PROFILE, intent="x", sql="SELECT 1", plan=None,
                    verdict={"ok": False, "flags": [{"severity": "high"}], "notes": "bad"},
                    row_count=0)
        assert cur.execute.call_args[0][1][-1] is True


class TestRunAdhocAudited:
    def test_clean_query_passes_and_is_audited(self):
        conn = MagicMock()
        with patch("lib.sql_guard.run_adhoc", return_value={
                "ok": True, "error": None,
                "rows": [{"recovery_score_pct": 70}], "columns": ["recovery_score_pct"],
                "sql": "SELECT recovery_score_pct FROM whoop_cycles WHERE profile_id=%(profile_id)s "
                       "AND cycle_start >= current_date - interval '7 days'",
                "traps": {}, "warning": None}), \
             patch("lib.sql_guard.audit_query") as audit:
            res = run_adhoc_audited(conn, PROFILE, "SELECT ...", intent="recovery")
        assert res["quality"]["ok"] is True
        assert audit.called   # every ad-hoc query is logged

    def test_fanout_query_flagged_and_audited(self):
        conn = MagicMock()
        with patch("lib.sql_guard.run_adhoc", return_value={
                "ok": True, "error": None, "rows": [{"count": 9999}], "columns": ["count"],
                "sql": "SELECT count(*) FROM whoop_cycles c JOIN whoop_workouts w "
                       "WHERE c.profile_id=%(profile_id)s",
                "traps": {}, "warning": None}), \
             patch("lib.sql_guard.audit_query") as audit:
            res = run_adhoc_audited(conn, PROFILE, "SELECT ...", intent="count")
        assert res["quality"]["ok"] is False
        assert any(f["code"] == "join_without_key" for f in res["quality"]["flags"])
        assert audit.called
