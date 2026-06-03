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


_REF_RE = re.compile(r"\b(?:from|join)\s+(?:public\.)?([a-z_][a-z0-9_]*)", re.IGNORECASE)


def referenced_tables(sql: str) -> list[str]:
    """Extract table/view names referenced after FROM/JOIN (best-effort)."""
    return sorted({m.group(1).lower() for m in _REF_RE.finditer(_strip_sql_comments(sql))})


def relevant_traps(conn, sql: str) -> dict:
    """Return table + column COMMENTs (from pg_description) for every table the query
    references that carry a TRAP/JOIN-TRAP marker. This puts the schema map's hazards
    IN FRONT of the model at compose/validate time — the comments become active, not
    passive docs. {table: {"table": <comment>, "columns": {col: comment, ...}}}."""
    tables = referenced_tables(sql)
    if not tables:
        return {}
    cur = conn.cursor()
    out: dict = {}
    for t in tables:
        # table comment
        try:
            cur.execute("SELECT obj_description(('public.'||%s)::regclass)", (t,))
            tc = (cur.fetchone() or [None])[0]
        except Exception:
            conn.rollback(); continue
        # column comments that mention a TRAP
        cur.execute("""
            SELECT a.attname, col_description(a.attrelid, a.attnum)
            FROM pg_attribute a
            JOIN pg_class cl ON cl.oid=a.attrelid
            JOIN pg_namespace n ON n.oid=cl.relnamespace
            WHERE n.nspname='public' AND cl.relname=%s AND a.attnum>0 AND NOT a.attisdropped
              AND col_description(a.attrelid,a.attnum) ILIKE '%%TRAP%%'
        """, (t,))
        cols = {name: c for name, c in cur.fetchall() if c}
        if (tc and "TRAP" in tc.upper()) or cols:
            out[t] = {"table": tc if (tc and "TRAP" in tc.upper()) else None, "columns": cols}
    return out


def run_adhoc(conn, sql: str, limit: int = 500) -> dict:
    """Validate then execute a read-only ad-hoc query.

    Returns {ok, error, columns, rows, sql, traps, warning}:
      - traps: the TRAP column/table comments for every referenced table (active schema map).
      - warning: set when 0 rows come back — a silent-empty result is the failure mode
        EXPLAIN can't catch (e.g. a wrong join key). The skill must re-check the traps,
        not report "no data".
    """
    traps = relevant_traps(conn, sql)
    v = validate_readonly_sql(conn, sql, limit)
    if not v["ok"]:
        return {"ok": False, "error": v["error"], "columns": [], "rows": [], "sql": v["sql"], "traps": traps, "warning": None}
    import psycopg2.extras
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(v["sql"])
    rows = [dict(r) for r in cur.fetchall()]
    cols = list(rows[0].keys()) if rows else []
    # 0 rows OR a single all-NULL aggregate row = suspicious empty result
    empty = (not rows) or (len(rows) == 1 and all(v is None for v in rows[0].values()))
    warning = None
    if empty and traps:
        warning = ("0 rows / all-NULL aggregate. EXPLAIN passed, so this is likely a JOIN/FILTER "
                   "trap, not absent data. Check the TRAP comments on: " + ", ".join(traps) +
                   " (e.g. join whoop_journal↔whoop_cycles on cycle_start::date). Re-check before "
                   "answering 'no data'.")
    return {"ok": True, "error": None, "columns": cols, "rows": rows, "sql": v["sql"],
            "traps": traps, "warning": warning}


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
