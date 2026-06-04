"""Unit tests for the V3-6 sql_guard quality layer + audit."""
from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch

from lib.sql_guard import quality_check, log_query, run_adhoc_audited

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


def _insert_call(cur):
    """Find the INSERT execute() among the savepoint wrapper calls."""
    for call in cur.execute.call_args_list:
        if "INSERT INTO query_audit" in call[0][0]:
            return call
    raise AssertionError("no INSERT INTO query_audit was executed")


class TestLogQuery:
    def test_adhoc_insert_no_returning_and_kind(self):
        conn = MagicMock(); cur = MagicMock(); conn.cursor.return_value = cur
        log_query(conn, PROFILE, kind="adhoc", name_or_sql="SELECT 1",
                  params={"intent": "recovery trend"},
                  quality_verdict={"ok": True, "flags": [], "notes": "clean"}, row_count=5)
        call = _insert_call(cur)
        sql, params = call[0][0], call[0][1]
        assert "INSERT INTO query_audit" in sql and "RETURNING" not in sql.upper()
        assert params[1] == "adhoc"        # kind
        assert params[-1] is False         # flagged = verdict not-ok
        # savepoint discipline: INSERT is wrapped so audit never breaks the read path
        executed = [c[0][0] for c in cur.execute.call_args_list]
        assert any("SAVEPOINT _audit" in e for e in executed)

    def test_catalog_has_no_verdict_and_never_flagged(self):
        conn = MagicMock(); cur = MagicMock(); conn.cursor.return_value = cur
        log_query(conn, PROFILE, kind="catalog", name_or_sql="v_recovery_30d",
                  params={"days": 30}, row_count=12)
        params = _insert_call(cur)[0][1]
        assert params[1] == "catalog"
        assert params[4] is None           # quality_verdict NULL for catalog
        assert params[-1] is False         # never flagged

    def test_flagged_true_when_high_flag(self):
        conn = MagicMock(); cur = MagicMock(); conn.cursor.return_value = cur
        log_query(conn, PROFILE, kind="adhoc", name_or_sql="SELECT 1",
                  params={"intent": "x"},
                  quality_verdict={"ok": False, "flags": [{"severity": "high"}], "notes": "bad"},
                  row_count=0)
        assert _insert_call(cur)[0][1][-1] is True

    def test_insert_failure_rolls_back_to_savepoint_not_raise(self):
        # a read-only connection raises on INSERT; audit must swallow + savepoint-rollback
        conn = MagicMock(); cur = MagicMock(); conn.cursor.return_value = cur

        def fail_on_insert(sql, *a, **k):
            if "INSERT INTO query_audit" in sql:
                raise RuntimeError("cannot execute INSERT in a read-only transaction")
        cur.execute.side_effect = fail_on_insert
        # must not raise
        log_query(conn, PROFILE, kind="catalog", name_or_sql="v_recovery_30d",
                  params={"days": 30}, row_count=0)
        executed = [c[0][0] for c in cur.execute.call_args_list]
        assert any("ROLLBACK TO SAVEPOINT _audit" in e for e in executed)


class TestRunAdhocAudited:
    def test_clean_query_passes_and_lands_adhoc_row(self):
        conn = MagicMock()
        with patch("lib.sql_guard.run_adhoc", return_value={
                "ok": True, "error": None,
                "rows": [{"recovery_score_pct": 70}], "columns": ["recovery_score_pct"],
                "sql": "SELECT recovery_score_pct FROM whoop_cycles WHERE profile_id=%(profile_id)s "
                       "AND cycle_start >= current_date - interval '7 days'",
                "traps": {}, "warning": None}), \
             patch("lib.sql_guard.log_query") as logq:
            res = run_adhoc_audited(conn, PROFILE, "SELECT ...", intent="recovery")
        assert res["quality"]["ok"] is True
        assert logq.called and logq.call_args.kwargs["kind"] == "adhoc"   # one ad-hoc row lands

    def test_fanout_query_flagged_and_audited(self):
        conn = MagicMock()
        with patch("lib.sql_guard.run_adhoc", return_value={
                "ok": True, "error": None, "rows": [{"count": 9999}], "columns": ["count"],
                "sql": "SELECT count(*) FROM whoop_cycles c JOIN whoop_workouts w "
                       "WHERE c.profile_id=%(profile_id)s",
                "traps": {}, "warning": None}), \
             patch("lib.sql_guard.log_query") as logq:
            res = run_adhoc_audited(conn, PROFILE, "SELECT ...", intent="count")
        assert res["quality"]["ok"] is False
        assert any(f["code"] == "join_without_key" for f in res["quality"]["flags"])
        assert logq.called
        assert logq.call_args.kwargs["quality_verdict"]["ok"] is False   # verdict logged


class TestCatalogRunLands:
    """A catalog run (lib/views.run_view) must land exactly one kind='catalog' row."""

    def test_run_view_logs_catalog_row(self):
        from lib import views
        conn = MagicMock(); cur = MagicMock()
        conn.cursor.return_value = cur
        cur.fetchall.return_value = [{"date": "2026-06-01", "asleep_duration_min": 420}]
        with patch("lib.sql_guard.log_query") as logq:
            out = views.run_view(conn, "v_sleep_debt_30d", PROFILE, days=30)  # no summary_sql
        assert out["name"] == "v_sleep_debt_30d"
        assert logq.called
        kw = logq.call_args.kwargs
        assert kw["kind"] == "catalog"
        assert kw["name_or_sql"] == "v_sleep_debt_30d"
        assert kw["params"] == {"days": 30}            # profile_id stripped from params
        assert "quality_verdict" not in kw or kw.get("quality_verdict") is None
