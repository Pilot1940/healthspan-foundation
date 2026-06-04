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


# ===========================================================================
# Quality layer (V3-6) — heuristics on AD-HOC (non-catalog) queries + audit log.
# Catalog queries (lib/views) are pre-vetted and skip this entirely.
# ===========================================================================

# time-series tables: an aggregate over these without a date window is suspect
_TIMESERIES = ("whoop_cycles", "whoop_sleeps", "whoop_workouts", "biomarkers",
               "food_logs", "supplement_intake_logs", "daily_logs")
# columns that are legitimately NULL a lot — averaging them silently is the recovery trap
_NULLABLE_AGG_COLS = ("recovery_score_pct", "hrv_ms", "sleep_performance_pct",
                      "sleep_debt_min", "calories")
_AGG = re.compile(r"\b(avg|sum|count|min|max)\s*\(", re.IGNORECASE)
_JOIN = re.compile(r"\bjoin\b", re.IGNORECASE)
_ON_OR_USING = re.compile(r"\b(on|using)\b", re.IGNORECASE)
_HAS_DATE_FILTER = re.compile(
    r"(interval|current_date|now\(\)|>=|<=|between|::date|_start|measured_at|log_date|taken_at)",
    re.IGNORECASE)
_COMMA_JOIN = re.compile(r"\bfrom\s+[a-z_][\w]*\s*(?:\s+\w+)?\s*,\s*[a-z_]", re.IGNORECASE)


def quality_check(sql: str, *, intent: str = "", rows: list | None = None,
                  empty: bool | None = None) -> dict:
    """Heuristic quality pass on an ad-hoc query. Returns {ok, flags:[{code,severity,note}], notes}.

    Flags (severity high = likely-wrong result, blocks 'ok'; low = advisory):
      * join_without_key / comma_join (fan-out / cartesian risk)            — high
      * aggregate_over_nullable (avg/sum of a NULL-heavy col w/o FILTER)    — high
      * empty_result (0 rows / all-NULL aggregate — the silent-empty trap)  — high
      * no_profile_scope (no profile_id filter — usually RLS covers it)     — low
      * aggregate_no_time_window (aggregate over a time-series table, no date)— low
    """
    body = _strip_sql_comments(sql or "")
    low = body.lower()
    flags: list[dict] = []

    def add(code, sev, note):
        flags.append({"code": code, "severity": sev, "note": note})

    # fan-out: a JOIN with no ON/USING, or an old-style comma join in FROM
    n_join = len(_JOIN.findall(body))
    n_on = len(_ON_OR_USING.findall(body))
    if n_join and n_on < n_join:
        add("join_without_key", "high",
            f"{n_join} JOIN(s) but only {n_on} ON/USING — a keyless join fans out rows "
            f"and inflates aggregates.")
    if _COMMA_JOIN.search(body):
        add("comma_join", "high", "comma-style join in FROM (cartesian product risk).")

    # aggregate over a known NULL-heavy column without FILTER (the recovery=0 trap)
    if _AGG.search(body) and "filter" not in low:
        hit = [c for c in _NULLABLE_AGG_COLS if re.search(rf"\b(avg|sum)\s*\(\s*{c}\b", low)]
        if hit:
            add("aggregate_over_nullable", "high",
                f"aggregating NULL-heavy column(s) {hit} without FILTER (WHERE … IS NOT NULL) — "
                f"NULLs distort the result (e.g. no-sleep cycles).")

    # scoping advisories
    if "profile_id" not in low:
        add("no_profile_scope", "low",
            "no profile_id filter — RLS still scopes, but be explicit for shared-table aggregates.")
    refs = referenced_tables(body)
    if _AGG.search(body) and any(t in refs for t in _TIMESERIES) and not _HAS_DATE_FILTER.search(body):
        add("aggregate_no_time_window", "low",
            "aggregate over a time-series table with no date window — likely unintended all-time scope.")

    # empty / silent-empty result
    if empty is None and rows is not None:
        empty = (not rows) or (len(rows) == 1 and all(v is None for v in rows[0].values()))
    if empty:
        add("empty_result", "high",
            "0 rows / all-NULL aggregate — EXPLAIN passed, so suspect a JOIN/FILTER trap, "
            "not absent data. Re-check before answering 'no data'.")

    high = [f for f in flags if f["severity"] == "high"]
    return {"ok": not high, "flags": flags,
            "notes": ("; ".join(f["note"] for f in high) if high else "clean")}


def log_query(conn, profile_id: str, *, kind: str, name_or_sql: str,
              params: dict | None = None, quality_verdict: dict | None = None,
              row_count: int | None = None) -> None:
    """Append ONE query to query_audit — used for BOTH catalog and ad-hoc reads (migration
    025). Non-blocking: wrapped in a SAVEPOINT so an audit failure (e.g. a read-only
    connection, or a non-maintainer's RLS) NEVER breaks the read path. No RETURNING —
    maintainer-only SELECT applies; INSERT is owner-scoped.

      kind='catalog' → name_or_sql=view name, params=view params, quality_verdict=NULL.
      kind='adhoc'   → name_or_sql=the SQL, params={"intent": ...}, quality_verdict=verdict.

    flagged is derived from the verdict (any high flag → True); catalog views are pre-vetted
    so they are never flagged.
    """
    import json
    flagged = bool(quality_verdict is not None and not quality_verdict.get("ok", True))
    cur = conn.cursor()
    try:
        cur.execute("SAVEPOINT _audit")
        cur.execute(
            """INSERT INTO query_audit
                 (profile_id, kind, name_or_sql, params, quality_verdict, row_count, flagged)
               VALUES (%s, %s, %s, %s::jsonb, %s::jsonb, %s, %s)""",
            (profile_id, kind, name_or_sql, json.dumps(params or {}),
             (json.dumps(quality_verdict) if quality_verdict is not None else None),
             row_count, flagged),
        )
        cur.execute("RELEASE SAVEPOINT _audit")
    except Exception:
        # audit is best-effort: roll back only the failed INSERT, leave the read intact.
        try:
            cur.execute("ROLLBACK TO SAVEPOINT _audit")
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass


def run_adhoc_audited(conn, profile_id: str, sql: str, *, intent: str = "",
                      limit: int = 500) -> dict:
    """run_adhoc + quality_check + audit. The path for NON-catalog queries: every ad-hoc
    query is quality-checked and logged to query_audit. Returns the run_adhoc result plus
    a 'quality' verdict. (Catalog views bypass this — they're pre-vetted.)"""
    res = run_adhoc(conn, sql, limit)
    # quality-check the SAME query that actually ran (and gets audited), not the raw input
    checked_sql = res.get("sql") or sql
    verdict = quality_check(
        checked_sql, intent=intent, rows=res.get("rows"),
        empty=(res.get("warning") is not None) or (res["ok"] and not res.get("rows")),
    )
    if not res["ok"]:
        # validation already failed (write/typo/multi-stmt); record that as the verdict
        verdict = {"ok": False,
                   "flags": [{"code": "invalid_sql", "severity": "high", "note": res["error"]}],
                   "notes": res["error"]}
    # log_query is itself non-blocking (savepoint); auditing never breaks the read path.
    log_query(conn, profile_id, kind="adhoc", name_or_sql=res.get("sql") or sql,
              params={"intent": intent}, quality_verdict=verdict,
              row_count=len(res.get("rows") or []))
    res["quality"] = verdict
    return res
