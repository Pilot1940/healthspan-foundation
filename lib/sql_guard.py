"""lib/sql_guard.py — two safety layers on the skill's READ path (PROMPT 17).

1. validate_readonly_sql(conn, sql) — for ANY ad-hoc (non-catalog) query:
   - reject unless a SINGLE read-only statement (SELECT or leading WITH … SELECT);
     no INSERT/UPDATE/DELETE/DDL, no multiple statements, no trailing ';' tricks.
   - run EXPLAIN (NOT EXPLAIN ANALYZE — that would EXECUTE) on the SCOPED connection,
     so a hallucinated column/table/typo fails HERE, before any data is returned, and
     under the correct RLS identity.
   - inject a LIMIT by wrapping (so it applies even with CTEs/unions).
   - return {ok, error, plan, sql} — the skill surfaces `error` so the model self-corrects.

2. cite guard — best-effort: trace_numbers(answer, rows) flags figures in a drafted
   answer that don't appear in the returned data. SOFT warning (rounding / derived %
   make a hard guarantee brittle). The hard rule lives in SKILL.md: every number cites
   its source (table + date); an untraceable number is re-queried or not stated.
   This is BaySys A9 (LLMs CITE, NEVER INVENT) applied to health.
"""
from __future__ import annotations

import re

# write / DDL keywords that must never appear as the statement verb
_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|truncate|drop|alter|create|grant|revoke|"
    r"comment|copy|merge|call|do|vacuum|reindex|refresh|set|reset)\b",
    re.IGNORECASE,
)
_LEADING = re.compile(r"^\s*(with|select)\b", re.IGNORECASE)


def _strip_sql_comments(sql: str) -> str:
    """Remove -- line comments and /* */ block comments (so they can't hide tricks)."""
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    sql = re.sub(r"--[^\n]*", " ", sql)
    return sql


def validate_readonly_sql(conn, sql: str, limit: int = 500) -> dict:
    """Validate + EXPLAIN an ad-hoc read query on the scoped connection.

    Returns {"ok": bool, "error": str|None, "plan": str|None, "sql": str}.
    `sql` in the result is the LIMIT-wrapped form actually validated.
    """
    raw = (sql or "").strip()
    cleaned = _strip_sql_comments(raw).strip()

    if not cleaned:
        return {"ok": False, "error": "empty query", "plan": None, "sql": raw}

    # single statement only: reject any ';' except an optional trailing one
    body = cleaned[:-1] if cleaned.endswith(";") else cleaned
    if ";" in body:
        return {"ok": False, "error": "multiple statements not allowed", "plan": None, "sql": raw}

    # must start with SELECT or WITH (CTE) — abnormal_labs etc. are CTEs
    if not _LEADING.match(body):
        return {"ok": False, "error": "only a single SELECT (or WITH … SELECT) is allowed",
                "plan": None, "sql": raw}

    # no write/DDL verbs anywhere (comments already stripped). string literals could
    # contain these words, but EXPLAIN-not-execute + read-only role make that harmless;
    # this is a fast reject for the obvious cases.
    if _FORBIDDEN.search(body):
        return {"ok": False, "error": "write/DDL keyword detected — read-only queries only",
                "plan": None, "sql": raw}

    # wrap to inject LIMIT robustly (works with CTEs/unions/existing LIMIT subqueries)
    wrapped = f"SELECT * FROM (\n{body}\n) _guard LIMIT {int(limit)}"

    cur = conn.cursor()
    try:
        cur.execute("SAVEPOINT _sqlguard")
        cur.execute("EXPLAIN " + wrapped)   # plain EXPLAIN — does NOT execute
        plan = "\n".join(r[0] for r in cur.fetchall())
        cur.execute("RELEASE SAVEPOINT _sqlguard")
        return {"ok": True, "error": None, "plan": plan, "sql": wrapped}
    except Exception as e:
        try:
            cur.execute("ROLLBACK TO SAVEPOINT _sqlguard")
        except Exception:
            pass
        # EXPLAIN error = hallucinated column/table/typo — surface for self-correction
        return {"ok": False, "error": str(e).splitlines()[0], "plan": None, "sql": wrapped}


def run_adhoc(conn, sql: str, limit: int = 500) -> dict:
    """Validate then execute a read-only ad-hoc query. Returns {ok, error, columns, rows, sql}."""
    v = validate_readonly_sql(conn, sql, limit)
    if not v["ok"]:
        return {"ok": False, "error": v["error"], "columns": [], "rows": [], "sql": v["sql"]}
    import psycopg2.extras
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(v["sql"])
    rows = [dict(r) for r in cur.fetchall()]
    cols = list(rows[0].keys()) if rows else []
    return {"ok": True, "error": None, "columns": cols, "rows": rows, "sql": v["sql"]}


# ---------------------------------------------------------------------------
# Cite guard (best-effort) — A9 for health: every number must trace to a row.
# ---------------------------------------------------------------------------

_NUM = re.compile(r"-?\d+(?:\.\d+)?")


def _numbers_in(text: str) -> set[str]:
    return {m.group(0) for m in _NUM.finditer(text or "")}


def trace_numbers(answer: str, rows: list[dict], tol: float = 0.05) -> dict:
    """Best-effort check: which numbers in `answer` appear in the returned `rows`?

    Returns {"traceable": [...], "untraceable": [...]}. SOFT — rounding/derived %
    legitimately won't match exactly, so untraceable is a WARNING to re-check, not a
    hard failure. The skill's discipline (cite table+date per figure) is the real guard.
    """
    # collect every numeric token present in the data (as floats, rounded set)
    data_nums: set[float] = set()
    for r in rows:
        for v in r.values():
            if isinstance(v, (int, float)):
                data_nums.add(round(float(v), 2))
            else:
                for tok in _numbers_in(str(v)):
                    try:
                        data_nums.add(round(float(tok), 2))
                    except ValueError:
                        pass

    traceable, untraceable = [], []
    for tok in _numbers_in(answer):
        try:
            val = round(float(tok), 2)
        except ValueError:
            continue
        # match if within tolerance of any data number (handles rounding)
        hit = any(abs(val - d) <= max(tol, abs(d) * tol) for d in data_nums)
        (traceable if hit else untraceable).append(tok)
    return {"traceable": sorted(set(traceable)), "untraceable": sorted(set(untraceable))}
