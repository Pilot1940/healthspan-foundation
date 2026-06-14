"""analysis/supplement_summary.py — current regimens + on/off + adherence (V3-5).

Lists active regimens (with derived on/off status from the v_supplement_onoff view) and
computes simple adherence from supplement_intake_logs over a recent window: distinct days
a supplement was actually logged / days in the window. Deterministic; the skill narrates.
"""
from __future__ import annotations

import psycopg2.extras

from lib.views import run_view


def _adherence(conn, profile_id, *, days: int):
    """{supplement_id: distinct_days_logged} over the last `days` (RLS-scoped)."""
    cur = conn.cursor()
    cur.execute(
        """SELECT supplement_id, count(DISTINCT taken_at::date) AS d
           FROM supplement_intake_logs
           WHERE profile_id=%s AND taken_at >= CURRENT_DATE - (%s||' days')::interval
             AND voided_at IS NULL
           GROUP BY supplement_id""",
        (profile_id, days),
    )
    return {str(r[0]): r[1] for r in cur.fetchall()}


def summary(conn, profile_id, *, days: int = 14) -> dict:
    """Active regimens with on/off status + adherence% over the window.

    Adherence is only meaningful for daily regimens; for cyclical ones we report the
    raw logged-days and leave pct None (the on/off pattern, not a flat %, is the truth).
    """
    if not hasattr(conn, "cursor"):
        raise RuntimeError(
            "supplement_summary requires a direct_role (psycopg2) connection — it runs raw "
            "SQL that supabase_client/PostgREST mode cannot execute. Run from Claude Code/local "
            "(direct_role); App mode supports the REST-backed brief instead."
        )
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        """SELECT r.id, r.supplement_id, s.display_name AS supplement,
                  r.dose_amount, r.dose_unit, array_to_string(r.timing,', ') AS timing,
                  r.frequency, r.status, r.cycle_days_on, r.cycle_days_off, r.purpose
           FROM supplement_regimens r
           JOIN supplements s ON s.id = r.supplement_id
           WHERE r.profile_id=%s AND r.status='active'
           ORDER BY s.display_name""",
        (profile_id,),
    )
    regimens = [dict(r) for r in cur.fetchall()]
    logged = _adherence(conn, profile_id, days=days)

    items = []
    for r in regimens:
        days_logged = logged.get(str(r["supplement_id"]), 0)
        is_daily = (r.get("frequency") == "daily" and not r.get("cycle_days_off"))
        pct = round(100.0 * days_logged / days, 0) if is_daily else None
        items.append({
            "supplement": r["supplement"],
            "dose": f"{r['dose_amount']}{(' '+r['dose_unit']) if r.get('dose_unit') else ''}".strip(),
            "timing": r["timing"], "frequency": r["frequency"], "purpose": r.get("purpose"),
            "days_logged": days_logged, "window_days": days, "adherence_pct": pct,
        })

    n_low = sum(1 for i in items if i["adherence_pct"] is not None and i["adherence_pct"] < 70)
    narrative = (f"{len(items)} active regimen(s) over the last {days}d"
                 + (f"; {n_low} below 70% adherence." if n_low else
                    "; adherence on track where measurable." if items else "."))
    return {"window_days": days, "regimens": items, "low_adherence_count": n_low,
            "narrative": narrative}
