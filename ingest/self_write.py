"""ingest/self_write.py — direct self-write path (no maintainer RPC).

WHY: `maintainer_ingest_*` is maintainer-gated — a NON-maintainer (e.g. Dea) calling
it gets `unauthorized`. So a non-maintainer logs their OWN rows by inserting directly:
`authenticated` holds INSERT, and RLS `has_profile_access(profile_id)` scopes every
insert to the caller's accessible profiles. A non-maintainer's only accessible profile
is themselves, so they can ONLY write their own data — the DB enforces it, not this code.
A maintainer (PC) targeting a family member's profile_id also works (has_profile_access
is true for the whole family); PC's food/supplement/biomarker logging otherwise keeps the
maintainer RPC path (staging, plausibility gates). This module is also the ONLY write path
for `strength_logs` (mig 063 added no ingest RPC) — used by everyone.

GENERATED columns are NEVER sent (the DB computes them):
  * supplement_intake_logs.taken_on  (from taken_at)
  * strength_logs.performed_on    (from performed_at)
NOTE: food_logs.log_date is NOT generated (no default, is_generated=NEVER). log_food()
sets it explicitly to the UTC date of logged_at — mirroring maintainer_ingest_food. Omitting
it (as this module previously did) inserts NULL log_date, which silently drops the row from
every `log_date = today` query (daily totals, orphan sweeps).

Driver-agnostic: accepts whatever handle the skill holds —
  * lib.db_rest.DbRest      → .insert()           (App / PostgREST; the primary path)
  * psycopg2 connection     → parameterised INSERT (local / direct_role; PC)
  * supabase Client         → .table().insert()   (fallback if the skill kept the client)
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def _clean(row: dict) -> dict:
    """Drop None values so DB defaults / nullable columns aren't overwritten with NULL."""
    return {k: v for k, v in row.items() if v is not None}


def _utc_date(ts: Any) -> str:
    """UTC calendar date (YYYY-MM-DD) of a timestamp — str/ISO or datetime; None → today UTC.
    Mirrors maintainer_ingest_food's `(logged_at AT TIME ZONE 'UTC')::date` so both write paths
    agree on food_logs.log_date (which is NOT a generated column)."""
    if ts is None:
        return datetime.now(timezone.utc).date().isoformat()
    dt = ts if isinstance(ts, datetime) else None
    if dt is None:
        try:
            dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        except ValueError:
            return datetime.now(timezone.utc).date().isoformat()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).date().isoformat()


def _insert(handle: Any, table: str, row: dict) -> str | None:
    """Insert one row through whichever driver `handle` is. Returns the new id (best effort)."""
    row = _clean(row)
    # DbRest (PostgREST) — the documented App path.
    if hasattr(handle, "insert") and hasattr(handle, "_base"):
        res = handle.insert(table, row)
        return str(res["id"]) if isinstance(res, dict) and res.get("id") else None
    # psycopg2 connection — local/direct_role (PC).
    if hasattr(handle, "cursor"):
        cols = list(row.keys())
        ph = ", ".join(["%s"] * len(cols))
        cur = handle.cursor()
        cur.execute(
            f'INSERT INTO public.{table} ({", ".join(cols)}) VALUES ({ph}) RETURNING id',
            [row[c] for c in cols],
        )
        new_id = cur.fetchone()[0]
        handle.commit()
        return str(new_id)
    # supabase Client fallback.
    if hasattr(handle, "table"):
        res = handle.table(table).insert(row).execute()
        data = getattr(res, "data", None)
        return str(data[0]["id"]) if data else None
    raise TypeError(f"self_write: unsupported handle type {type(handle)!r}")


def log_food(handle, profile_id, *, description, meal_type, calories=None,
             protein_g=None, carbs_g=None, fat_g=None, fiber_g=None, foods=None,
             logged_at=None, meal_time=None, location=None, notes=None,
             source="skill") -> str | None:
    """Insert one food_logs row for `profile_id`. meal_type must be one of
    breakfast|lunch|dinner|snack|drink|supplement (DB CHECK). log_date is NOT generated —
    set here to the UTC date of logged_at (mirrors maintainer_ingest_food)."""
    return _insert(handle, "food_logs", {
        "profile_id": profile_id, "description": description, "meal_type": meal_type,
        "calories": calories, "protein_g": protein_g, "carbs_g": carbs_g,
        "fat_g": fat_g, "fiber_g": fiber_g, "foods": foods, "logged_at": logged_at,
        "log_date": _utc_date(logged_at),
        "meal_time": meal_time, "location": location, "notes": notes, "source": source,
    })


def log_supplement(handle, profile_id, *, supplement_id, taken_at, regimen_id=None,
                   dose_amount=None, dose_unit=None, notes=None,
                   source="skill") -> str | None:
    """Insert one supplement_intake_logs row. source must be in
    manual|journal|skill|csv|photo|telegram (DB CHECK). taken_on is GENERATED."""
    return _insert(handle, "supplement_intake_logs", {
        "profile_id": profile_id, "supplement_id": supplement_id, "taken_at": taken_at,
        "regimen_id": regimen_id, "dose_amount": dose_amount, "dose_unit": dose_unit,
        "notes": notes, "source": source,
    })


def log_biomarker(handle, profile_id, *, metric_definition_id, value, measured_at,
                  unit=None, lab_name=None, qualifier=None, notes=None,
                  source="skill") -> str | None:
    """Insert one biomarkers row. qualifier (if set) must be one of <|>|<=|>=|~ (DB CHECK)."""
    return _insert(handle, "biomarkers", {
        "profile_id": profile_id, "metric_definition_id": metric_definition_id,
        "value": value, "measured_at": measured_at, "unit": unit, "lab_name": lab_name,
        "qualifier": qualifier, "notes": notes, "source": source,
    })


def log_strength(handle, profile_id, *, performed_at, exercise, modality=None,
                 load_value=None, load_unit=None, sets=None, reps=None, rir=None,
                 device_specific=False, notes=None, source="skill") -> str | None:
    """Insert one strength_logs row (mig 063). performed_on is GENERATED — never sent.
    load_unit is kg|lb|bodyweight (never 'plates'). A maintainer may pass any family
    profile_id; a non-maintainer's only writable profile_id is their own (RLS)."""
    return _insert(handle, "strength_logs", {
        "profile_id": profile_id, "performed_at": performed_at, "exercise": exercise,
        "modality": modality, "load_value": load_value, "load_unit": load_unit,
        "sets": sets, "reps": reps, "rir": rir, "device_specific": device_specific,
        "notes": notes, "source": source,
    })
