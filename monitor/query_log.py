"""monitor/query_log.py — session-start query observability (V3). MAINTAINER-ONLY.

Every skill read lands a row in query_audit (catalog runs via lib/views.run_view,
ad-hoc SELECTs via lib/sql_guard.run_adhoc_audited — migration 025). This module leads
the maintainer's session with a short digest of that trail: how many queries ran
recently, the split of catalog vs ad-hoc, the most-used catalog views, and — front and
centre — anything the quality layer flagged. Dea never sees it.

Defence in depth (mirrors monitor/ingest_health.py):
  * Skill-level: gated on is_maintainer() — a non-maintainer gets {"maintainer": False}.
  * DB-level: query_audit SELECT is maintainer-only (migration 022), so even if called
    the underlying reads return 0 rows for a non-maintainer.
"""
from __future__ import annotations

import psycopg2.extras


def is_maintainer(conn) -> bool:
    """Authoritative check on the SCOPED connection (matches the DB RLS predicate)."""
    cur = conn.cursor()
    cur.execute("SELECT public.is_maintainer()")
    return bool(cur.fetchone()[0])


def check(conn, *, recent_days: int = 7, flagged_limit: int = 10,
          top_views_limit: int = 5) -> dict:
    """Return {"maintainer": bool, "summary": {...}, "flagged": [...], "top_views": [...]}.

    For a non-maintainer, returns maintainer=False with empty payloads. For the maintainer,
    summarises the recent query trail and surfaces every flagged ad-hoc query.
    """
    if not is_maintainer(conn):
        return {"maintainer": False, "summary": {}, "flagged": [], "top_views": []}

    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    window = "%s days" % int(recent_days)

    # overall counts in the window, split by kind + flagged tally
    cur.execute(
        """SELECT
             count(*)                                   AS total,
             count(*) FILTER (WHERE kind='catalog')      AS catalog,
             count(*) FILTER (WHERE kind='adhoc')        AS adhoc,
             count(*) FILTER (WHERE flagged)             AS flagged,
             max(ts)                                     AS last_query_at
           FROM query_audit
           WHERE ts >= now() - %s::interval""",
        (window,),
    )
    summary = dict(cur.fetchone())
    summary["recent_days"] = recent_days

    # flagged ad-hoc queries (the quality layer caught something) — newest first
    cur.execute(
        """SELECT ts, profile_id, name_or_sql, params, quality_verdict
           FROM query_audit
           WHERE flagged AND ts >= now() - %s::interval
           ORDER BY ts DESC
           LIMIT %s""",
        (window, flagged_limit),
    )
    flagged = [dict(r) for r in cur.fetchall()]

    # most-used catalog views in the window
    cur.execute(
        """SELECT name_or_sql AS view, count(*) AS runs, max(ts) AS last_run
           FROM query_audit
           WHERE kind='catalog' AND ts >= now() - %s::interval
           GROUP BY name_or_sql
           ORDER BY runs DESC, last_run DESC
           LIMIT %s""",
        (window, top_views_limit),
    )
    top_views = [dict(r) for r in cur.fetchall()]

    return {"maintainer": True, "summary": summary,
            "flagged": flagged, "top_views": top_views}


def format_digest(result: dict) -> str:
    """Render check() into a one-glance maintainer line (empty string for non-maintainer
    or a silent window with nothing flagged)."""
    if not result.get("maintainer"):
        return ""
    s = result["summary"]
    total = s.get("total") or 0
    if not total:
        return ""
    parts = [f"{total} queries in {s['recent_days']}d "
             f"({s.get('catalog') or 0} catalog / {s.get('adhoc') or 0} ad-hoc)"]
    nflag = s.get("flagged") or 0
    if nflag:
        parts.append(f"⚠️ {nflag} flagged")
        for f in result.get("flagged", [])[:3]:
            note = (f.get("quality_verdict") or {}).get("notes", "")
            parts.append(f"   • {note}")
    if result.get("top_views"):
        tv = ", ".join(f"{v['view']}×{v['runs']}" for v in result["top_views"])
        parts.append(f"top views: {tv}")
    return "\n".join(parts)
