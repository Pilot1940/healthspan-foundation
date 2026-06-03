"""plan/training_plan.py — create + read training plans (V3-3).

A plan is a training_programs row (objective, target_event, dates, status) with ordered
program_phases. Each phase carries its prescription in ``weekly_template`` (jsonb) — e.g.
{"mon": "Zone2 60min", "wed": "VO2 4x4", ...}. program_workouts links CONCRETE performed
whoop_workouts to a phase (workout_id → whoop_workouts, NOT NULL) — it is NOT where the
prescription lives, so we never insert a placeholder workout for a not-yet-done session.

Standalone module; profile-scoped conn (RLS isolation). Client-side UUIDs.
"""
from __future__ import annotations

import json
import uuid as _uuid

_VALID_STATUS = ("planned", "active", "done", "abandoned")


def create_training_plan(conn, profile_id: str, name: str, *, objective: str | None = None,
                         target_event: str | None = None, start_date=None, end_date=None,
                         status: str = "planned", phases: list[dict] | None = None) -> dict:
    """Create a program + its ordered phases. Returns the assembled plan.

    ``phases``: [{name, ordinal?, start_date?, end_date?, weekly_template?}]. ``ordinal``
    defaults to list order. ``weekly_template`` (dict) holds the week's prescribed sessions.
    """
    if status not in _VALID_STATUS:
        raise ValueError(f"status {status!r} not in {_VALID_STATUS}")
    pid = str(_uuid.uuid4())
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO training_programs
             (id, profile_id, name, objective, target_event, start_date, end_date, status)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
        (pid, profile_id, name, objective, target_event, start_date, end_date, status),
    )
    created_phases = []
    for i, ph in enumerate(phases or []):
        ph_id = str(_uuid.uuid4())
        wt = ph.get("weekly_template")
        cur.execute(
            """INSERT INTO program_phases
                 (id, program_id, name, ordinal, start_date, end_date, weekly_template)
               VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb)""",
            (ph_id, pid, ph["name"], ph.get("ordinal", i),
             ph.get("start_date"), ph.get("end_date"),
             json.dumps(wt) if wt is not None else "{}"),
        )
        created_phases.append({"id": ph_id, "name": ph["name"],
                               "ordinal": ph.get("ordinal", i),
                               "weekly_template": wt or {}})
    return {"id": pid, "profile_id": profile_id, "name": name, "objective": objective,
            "target_event": target_event, "start_date": start_date, "end_date": end_date,
            "status": status, "phases": created_phases}


def list_training_plans(conn, profile_id: str) -> list[dict]:
    import psycopg2.extras
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        """SELECT id, name, objective, target_event, start_date, end_date, status, created_at
           FROM training_programs WHERE profile_id=%s
           ORDER BY (status='active') DESC, start_date DESC NULLS LAST, created_at DESC""",
        (profile_id,),
    )
    return [dict(r) for r in cur.fetchall()]


def get_training_plan(conn, profile_id: str, program_id: str) -> "dict|None":
    """Assemble a program with its ordered phases (and any linked concrete workouts)."""
    import psycopg2.extras
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        """SELECT id, name, objective, target_event, start_date, end_date, status
           FROM training_programs WHERE id=%s AND profile_id=%s""",
        (program_id, profile_id),
    )
    prog = cur.fetchone()
    if not prog:
        return None
    prog = dict(prog)
    cur.execute(
        """SELECT id, name, ordinal, start_date, end_date, weekly_template
           FROM program_phases WHERE program_id=%s ORDER BY ordinal""",
        (program_id,),
    )
    prog["phases"] = [dict(r) for r in cur.fetchall()]
    cur.execute(
        """SELECT pw.phase_id, pw.workout_id, pw.workout_type, pw.prescribed, pw.notes,
                  w.workout_start::date AS date, w.activity_name
           FROM program_workouts pw
           LEFT JOIN whoop_workouts w ON w.id = pw.workout_id
           WHERE pw.program_id=%s ORDER BY w.workout_start NULLS LAST""",
        (program_id,),
    )
    prog["linked_workouts"] = [dict(r) for r in cur.fetchall()]
    return prog


def set_status(conn, profile_id: str, program_id: str, status: str) -> None:
    if status not in _VALID_STATUS:
        raise ValueError(f"status {status!r} not in {_VALID_STATUS}")
    cur = conn.cursor()
    cur.execute(
        "UPDATE training_programs SET status=%s, updated_at=now() WHERE id=%s AND profile_id=%s",
        (status, program_id, profile_id),
    )
