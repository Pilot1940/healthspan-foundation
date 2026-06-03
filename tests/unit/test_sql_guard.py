"""Unit tests for lib/sql_guard.py — string-level validation (no DB)."""
from lib.sql_guard import validate_readonly_sql, trace_numbers
from unittest.mock import MagicMock


def _conn_explain_ok(plan="Seq Scan"):
    cur = MagicMock()
    cur.fetchall.return_value = [(plan,)]
    conn = MagicMock(); conn.cursor.return_value = cur
    return conn


def _conn_explain_fails(msg='column "nope" does not exist'):
    cur = MagicMock()
    def _exec(sql, *a):
        if sql.startswith("EXPLAIN"):
            raise Exception(msg)
    cur.execute.side_effect = _exec
    conn = MagicMock(); conn.cursor.return_value = cur
    return conn


class TestValidate:
    def test_plain_select_ok(self):
        r = validate_readonly_sql(_conn_explain_ok(), "SELECT 1")
        assert r["ok"] and "LIMIT 500" in r["sql"]

    def test_leading_with_cte_ok(self):
        # abnormal_labs is a CTE — must be allowed
        r = validate_readonly_sql(_conn_explain_ok(), "WITH x AS (SELECT 1 a) SELECT * FROM x")
        assert r["ok"]

    def test_insert_rejected(self):
        r = validate_readonly_sql(_conn_explain_ok(), "INSERT INTO biomarkers VALUES (1)")
        assert not r["ok"] and "read-only" in r["error"].lower() or "select" in r["error"].lower()

    def test_update_rejected(self):
        r = validate_readonly_sql(_conn_explain_ok(), "UPDATE biomarkers SET value=1")
        assert not r["ok"]

    def test_delete_rejected(self):
        r = validate_readonly_sql(_conn_explain_ok(), "DELETE FROM biomarkers")
        assert not r["ok"]

    def test_multiple_statements_rejected(self):
        r = validate_readonly_sql(_conn_explain_ok(), "SELECT 1; DROP TABLE biomarkers")
        assert not r["ok"] and "multiple" in r["error"].lower()

    def test_trailing_semicolon_ok(self):
        r = validate_readonly_sql(_conn_explain_ok(), "SELECT 1;")
        assert r["ok"]

    def test_comment_hidden_write_rejected(self):
        # a write hidden after a -- comment trick → comment stripped → still one stmt, but
        # the DELETE becomes visible and is rejected
        r = validate_readonly_sql(_conn_explain_ok(), "SELECT 1 --\nDELETE FROM biomarkers")
        assert not r["ok"]

    def test_explain_error_surfaces(self):
        # hallucinated column → EXPLAIN fails → error surfaced for self-correction
        r = validate_readonly_sql(_conn_explain_fails(), "SELECT nope FROM biomarkers")
        assert not r["ok"] and "does not exist" in r["error"]

    def test_empty_rejected(self):
        assert not validate_readonly_sql(_conn_explain_ok(), "   ")["ok"]


class TestCiteGuard:
    def test_traceable_numbers(self):
        rows = [{"recovery": 50.7, "hrv": 32.9}]
        out = trace_numbers("Recovery 50.7% with HRV 32.9", rows)
        assert "50.7" in out["traceable"] and not out["untraceable"]

    def test_untraceable_flagged(self):
        rows = [{"recovery": 50.7}]
        out = trace_numbers("Recovery 50.7% and testosterone 999", rows)
        assert "999" in out["untraceable"]

    def test_rounding_tolerance(self):
        rows = [{"avg": 50.66}]
        out = trace_numbers("about 50.7", rows)  # within tolerance
        assert "50.7" in out["traceable"]
