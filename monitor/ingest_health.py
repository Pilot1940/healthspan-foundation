"""monitor/ingest_health.py — session-start data-quality surfacing (V3-4). MAINTAINER-ONLY.

Data-quality is a maintainer concern (PC), not a per-user one. This module leads the
maintainer's session with anything needing attention: lab/food rows in staging awaiting
review, failed sync runs, recent per-record errors. Dea never sees it.

Defence in depth (blueprint §4.0):
  * Skill-level: gated on is_maintainer() — a non-maintainer gets {"maintainer": False}.
  * DB-level: even if called, the maintainer-only RLS on the four tables returns 0 rows
    for a non-maintainer (migration 022). Both are exercised below.
"""
from __future__ import annotations

import psycopg2.extras


def is_maintainer(conn) -> bool:
    """Authoritative check on the SCOPED connection (matches the DB RLS predicate)."""
    cur = conn.cursor()
    cur.execute("SELECT public.is_maintainer()")
    return bool(cur.fetchone()[0])


def check(conn, *, recent_days: int = 7) -> dict:
    """Return {"maintainer": bool, "items": [...]}.

    For a non-maintainer, returns maintainer=False with no items (the skill shows nothing
    of the machinery — just simple per-ingest outcomes elsewhere). For the maintainer,
    summarises staging backlog, failed runs, and recent errors.
    """
    if not is_maintainer(conn):
        return {"maintainer": False, "items": []}

    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    items: list[dict] = []

    # staged rows awaiting review (both staging queues)
    for tbl, label in (("stg_biomarker_review", "lab value"),
                       ("stg_food_log_review", "food log")):
        cur.execute(f"SELECT count(*) n FROM {tbl} WHERE status='pending'")
        n = cur.fetchone()["n"]
        if n:
            items.append({"kind": "staging", "severity": "warning",
                          "message": f"{n} {label}{'s' if n != 1 else ''} in staging awaiting your review.",
                          "count": n, "table": tbl})

    # failed / partially-failed sync runs in the window
    cur.execute(
        """SELECT count(*) runs, COALESCE(sum(records_failed),0) recs
           FROM wearable_sync_log
           WHERE started_at >= CURRENT_DATE - (%s||' days')::interval
             AND (status='failed' OR records_failed > 0)""",
        (recent_days,),
    )
    r = cur.fetchone()
    if r["runs"]:
        items.append({"kind": "sync_failure", "severity": "warning",
                      "message": f"{r['runs']} sync run(s) in the last {recent_days}d had failures "
                                 f"({r['recs']} record(s) failed).",
                      "runs": r["runs"], "records_failed": int(r["recs"])})

    # recent per-record errors (detail behind the failures)
    cur.execute(
        """SELECT error_code, count(*) n FROM wearable_sync_errors e
           JOIN wearable_sync_log l ON l.id = e.sync_log_id
           WHERE l.started_at >= CURRENT_DATE - (%s||' days')::interval
           GROUP BY error_code ORDER BY n DESC""",
        (recent_days,),
    )
    errs = cur.fetchall()
    if errs:
        items.append({"kind": "errors", "severity": "info",
                      "message": "Recent ingest errors: " +
                                 ", ".join(f"{e['n']}×{e['error_code']}" for e in errs),
                      "by_code": {e["error_code"]: e["n"] for e in errs}})

    return {"maintainer": True, "items": items}
