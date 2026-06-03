"""plan/goals.py — MULTIPLE concurrent goal tracking (V3-3).

Goals live in user_goals. Migration 020 dropped the one-active-per-program uniqueness,
so a profile may have any number of concurrent active goals (standalone or per-program).
Progress is computed per-goal from the goal's metric vs target/baseline — direction-aware
(raising VO2max and lowering ApoB both read as "% of the way there").

Standalone module: importable and runnable on its own. Profile-scoped psycopg2 conn
(RLS does isolation). Targets/units come from the caller (the skill reads them from the
context MD), never invented here.
"""
from __future__ import annotations

import uuid as _uuid

_GOAL_COLS = (
    "id", "profile_id", "metric_definition_id", "title", "description",
    "target_value", "target_unit", "target_date", "baseline_value",
    "program_id", "is_active",
)


def _resolve_metric(conn, metric_name: str | None):
    """metric_definitions.name → (id, unit) or (None, None). Exact match only; never guesses."""
    if not metric_name:
        return None, None
    cur = conn.cursor()
    cur.execute(
        "SELECT id, unit FROM metric_definitions WHERE is_active AND lower(name)=lower(%s) LIMIT 1",
        (metric_name,),
    )
    row = cur.fetchone()
    return (str(row[0]), row[1]) if row else (None, None)


def create_goal(conn, profile_id: str, title: str, *, metric_name: str | None = None,
                target_value=None, target_unit=None, target_date=None,
                baseline_value=None, program_id: str | None = None,
                description: str | None = None) -> dict:
    """Create one active goal. Multiple concurrent goals are allowed (020).

    If ``metric_name`` is given it must resolve to a metric_definitions row (so progress
    can be computed from that metric's readings); an unresolved name is left NULL and the
    goal is qualitative. ``target_unit`` defaults to the metric's catalog unit.
    """
    metric_id, cat_unit = _resolve_metric(conn, metric_name)
    gid = str(_uuid.uuid4())
    row = {
        "id": gid, "profile_id": profile_id, "metric_definition_id": metric_id,
        "title": title, "description": description,
        "target_value": target_value, "target_unit": target_unit or cat_unit,
        "target_date": target_date, "baseline_value": baseline_value,
        "program_id": program_id, "is_active": True,
    }
    cols = [c for c in _GOAL_COLS if row.get(c) is not None]
    ph = ", ".join(["%s"] * len(cols))
    cur = conn.cursor()
    cur.execute(
        f"INSERT INTO user_goals ({', '.join(cols)}) VALUES ({ph})",
        [row[c] for c in cols],
    )
    return {**row, "metric_name": metric_name}


def list_goals(conn, profile_id: str, *, active_only: bool = True) -> list[dict]:
    """All goals for the profile (newest first), each with its metric name."""
    import psycopg2.extras
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        f"""SELECT g.id, g.title, g.description, g.metric_definition_id,
                   m.name AS metric_name, g.target_value, g.target_unit, g.target_date,
                   g.baseline_value, g.program_id, g.is_active, g.progress_pct, g.created_at
            FROM user_goals g
            LEFT JOIN metric_definitions m ON m.id = g.metric_definition_id
            WHERE g.profile_id = %s {"AND g.is_active" if active_only else ""}
            ORDER BY g.created_at DESC""",
        (profile_id,),
    )
    return [dict(r) for r in cur.fetchall()]


def compute_progress(current, baseline, target) -> "float|None":
    """Direction-aware % toward target. None if not computable.

    Works for BOTH directions because numerator and denominator flip together:
      raise  (target>baseline): VO2 40→50, now 45 → (45-40)/(50-40) = 50%
      lower  (target<baseline): ApoB 100→70, now 85 → (85-100)/(70-100) = 50%
    Clamped to [0,100]. Requires baseline, target, current all present and
    target != baseline.
    """
    if current is None or baseline is None or target is None:
        return None
    current, baseline, target = float(current), float(baseline), float(target)
    if target == baseline:
        return None
    pct = (current - baseline) / (target - baseline) * 100.0
    return max(0.0, min(100.0, round(pct, 1)))


def _latest_metric_value(conn, profile_id: str, metric_id: str):
    """Latest biomarkers.value for this profile+metric (RLS-scoped). None if no reading."""
    cur = conn.cursor()
    cur.execute(
        """SELECT value FROM biomarkers
           WHERE profile_id=%s AND metric_definition_id=%s AND value IS NOT NULL
           ORDER BY measured_at DESC LIMIT 1""",
        (profile_id, metric_id),
    )
    row = cur.fetchone()
    return row[0] if row else None


def track_goals(conn, profile_id: str) -> list[dict]:
    """Each active goal with current metric value + computed progress.

    ``progress_pct`` is None when the goal has no metric, no baseline, or no reading yet
    (the skill then reports current-vs-target qualitatively rather than inventing a %).
    """
    out = []
    for g in list_goals(conn, profile_id, active_only=True):
        current = None
        if g["metric_definition_id"]:
            current = _latest_metric_value(conn, profile_id, g["metric_definition_id"])
        prog = compute_progress(current, g.get("baseline_value"), g.get("target_value"))
        out.append({
            **g,
            "current_value": current,
            "progress_pct": prog,
            "status": ("on_track" if prog is not None and prog >= 50
                       else "behind" if prog is not None else "tracking"),
        })
    return out
