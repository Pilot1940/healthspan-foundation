"""ingest/supplement.py — supplement intake logging.

Architecture §3.5:
  - Resolve supplement name via supplement_aliases (exact) or supplements.name.
  - Regimen-aware: look up the active supplement_regimens row for the profile
    and attach regimen_id automatically when caller doesn't supply one.
  - Write to supplement_intake_logs ON CONFLICT (profile_id, supplement_id, taken_on, source).
  - Low-confidence → stg_supplement_intake_review.
  - View and close active regimens.

Rules enforced:
  #1  No hardcoded thresholds.
  #2  Low-confidence → staging.
  #8  validate() before prod write (via ingest_record).
  #9  No hardcoded UUIDs.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from lib.contract import (
    confidence_min,
    ingest_record,
    open_sync_log,
    close_sync_log,
    stage,
    validate,
    write,
)

# Production unique constraint on supplement_intake_logs:
#   UNIQUE(profile_id, supplement_id, taken_on, source)
_CONFLICT_COLS = ["profile_id", "supplement_id", "taken_on", "source"]


# ---------------------------------------------------------------------------
# Regimen lookup
# ---------------------------------------------------------------------------


def find_active_regimen(conn, profile_id: str, supplement_id: str) -> "str | None":
    """Return the id of the active regimen for (profile, supplement), or None."""
    cur = conn.cursor()
    cur.execute(
        """SELECT id FROM supplement_regimens
           WHERE profile_id = %s
             AND supplement_id = %s
             AND status = 'active'
           ORDER BY start_date DESC
           LIMIT 1""",
        (profile_id, supplement_id),
    )
    row = cur.fetchone()
    return str(row[0]) if row else None


# ---------------------------------------------------------------------------
# Core ingest function
# ---------------------------------------------------------------------------


def ingest_supplement_row(payload: dict, conn, profile_id: str) -> dict:
    """Ingest one supplement intake through the full contract pipeline.

    payload keys:
        name        : str   supplement name or alias
        supplement_id: str  UUID — skip resolution if already known
        regimen_id  : str   UUID — auto-looked-up from active regimens if absent
        taken_at    : str   ISO datetime (defaults to now)
        taken_on    : str   ISO date (defaults to today, derived from taken_at)
        dose_amount : float
        dose_unit   : str
        source      : str   defaults to 'skill' (allowed: manual/journal/skill/csv)
        notes       : str

    Returns {"status": "inserted"|"updated"|"staged"|"failed", ...}
    """
    source = payload.get("source", "skill")   # DB CHECK: manual|journal|skill|csv
    taken_at_raw = payload.get("taken_at") or datetime.now(timezone.utc).isoformat()
    taken_on_raw = payload.get("taken_on")
    if not taken_on_raw:
        # derive date from taken_at
        try:
            taken_on_raw = str(datetime.fromisoformat(taken_at_raw).date())
        except (ValueError, TypeError):
            taken_on_raw = str(date.today())

    raw: dict[str, Any] = {
        "profile_id": profile_id,
        "name": payload.get("name"),
        "taken_at": taken_at_raw,
        "dose_amount": payload.get("dose_amount"),
        "dose_unit": payload.get("dose_unit"),
        "source": source,
        "notes": payload.get("notes"),
    }
    if payload.get("regimen_id"):
        raw["regimen_id"] = payload["regimen_id"]
    if payload.get("supplement_id"):
        raw["supplement_id"] = payload["supplement_id"]

    # -- resolve via contract -------------------------------------------------
    from lib.contract import resolve, _drop_none

    r = resolve(conn, "supplement", raw)
    supplement_id = r["fk"] or raw.get("supplement_id")
    confidence: float = r["confidence"]
    normalised = r["normalised"]

    # -- regimen-aware: auto-attach active regimen if not supplied ------------
    regimen_id = raw.get("regimen_id") or payload.get("regimen_id")
    if not regimen_id and supplement_id:
        regimen_id = find_active_regimen(conn, profile_id, supplement_id)

    # build the prod row from normalised output + extras
    prod_row: dict[str, Any] = _drop_none({
        "profile_id": profile_id,
        "supplement_id": supplement_id,
        "regimen_id": regimen_id,
        "taken_at": taken_at_raw,
        "taken_on": taken_on_raw,
        "dose_amount": normalised.get("dose_amount") or payload.get("dose_amount"),
        "dose_unit": normalised.get("dose_unit") or payload.get("dose_unit"),
        "source": source,
        "notes": payload.get("notes"),
    })

    # -- confidence gate (rule #1) -------------------------------------------
    threshold = confidence_min(conn)

    stg_source = {**raw, **prod_row}

    if supplement_id is None or confidence < threshold:
        stage(conn, "supplement_intake_logs", stg_source, confidence,
              raw_text=str(payload))
        return {
            "status": "staged",
            "reason": "low_confidence_or_unresolved",
            "confidence": confidence,
        }

    # -- validate before prod write (rule #8) --------------------------------
    ok, errors = validate(conn, "supplement_intake_logs", prod_row)
    if not ok:
        stage(conn, "supplement_intake_logs", stg_source, confidence,
              raw_text=str(payload))
        return {
            "status": "staged",
            "reason": "validation_failed",
            "errors": errors,
            "confidence": confidence,
        }

    # -- idempotent upsert ---------------------------------------------------
    # UNIQUE(profile_id, supplement_id, taken_on, source) — all 4 must be in prod_row
    for col in _CONFLICT_COLS:
        if col not in prod_row:
            return {
                "status": "failed",
                "error": f"missing conflict column {col!r} — cannot upsert",
            }

    result_status = write(conn, "supplement_intake_logs", prod_row, _CONFLICT_COLS)
    return {"status": result_status, "confidence": confidence}


# ---------------------------------------------------------------------------
# Regimen viewing + closing
# ---------------------------------------------------------------------------


def list_active_regimens(conn, profile_id: str) -> list[dict]:
    """Return a list of active supplement regimens for the profile."""
    cur = conn.cursor()
    cur.execute(
        """SELECT sr.id, s.display_name, sr.dose_amount, sr.dose_unit,
                  sr.frequency, sr.start_date, sr.status
           FROM supplement_regimens sr
           JOIN supplements s ON s.id = sr.supplement_id
           WHERE sr.profile_id = %s AND sr.status = 'active'
           ORDER BY sr.start_date DESC""",
        (profile_id,),
    )
    rows = cur.fetchall()
    return [
        {
            "id": str(r[0]),
            "supplement": r[1],
            "dose": f"{r[2]} {r[3]}",
            "frequency": r[4],
            "start_date": str(r[5]),
            "status": r[6],
        }
        for r in rows
    ]


def close_regimen(conn, regimen_id: str) -> bool:
    """Set a regimen's status to 'discontinued'. Returns True if found."""
    cur = conn.cursor()
    cur.execute(
        """UPDATE supplement_regimens
              SET status = 'discontinued', end_date = CURRENT_DATE, updated_at = now()
            WHERE id = %s AND status = 'active'
           RETURNING id""",
        (regimen_id,),
    )
    return cur.fetchone() is not None


# ---------------------------------------------------------------------------
# Interactive CLI entry point
# ---------------------------------------------------------------------------


def _ask(prompt: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    val = input(f"{prompt}{suffix}: ").strip()
    return val or (default or "")


def run_interactive(conn, profile_id: str) -> None:
    """Prompt the user to log supplement intake or manage regimens."""
    print("\n--- Supplement Tracker ---")
    print("  1) Log intake")
    print("  2) View active regimens")
    print("  3) Close a regimen")
    choice = _ask("Choice", "1")

    if choice == "2":
        regimens = list_active_regimens(conn, profile_id)
        if not regimens:
            print("No active regimens.")
        for r in regimens:
            print(f"  {r['id'][:8]}…  {r['supplement']}  {r['dose']}  {r['frequency']}  (since {r['start_date']})")
        return

    if choice == "3":
        regimen_id = _ask("Regimen ID (full UUID)")
        if close_regimen(conn, regimen_id):
            conn.commit()
            print("Regimen closed.")
        else:
            print("Regimen not found or already inactive.")
        return

    # -- Log intake (choice == "1") ------------------------------------------
    name = _ask("Supplement name or alias (e.g. Berberine, D3)")
    dose_amount_str = _ask("Dose amount (e.g. 500)")
    dose_unit = _ask("Dose unit (e.g. mg, IU)")
    taken_at = _ask(
        "Taken at (ISO datetime or leave blank for now)",
        datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00"),
    )
    notes = _ask("Notes (optional)", "")

    payload = {
        "name": name,
        "dose_amount": float(dose_amount_str) if dose_amount_str else None,
        "dose_unit": dose_unit or None,
        "taken_at": taken_at or None,
        "notes": notes or None,
    }

    result = ingest_supplement_row(payload, conn, profile_id)
    conn.commit()

    status = result["status"]
    conf = result.get("confidence", "?")
    conf_str = f"{conf:.0%}" if isinstance(conf, float) else str(conf)

    if status in ("inserted", "updated", "skipped"):
        print(f"\nLogged: {status}  (confidence {conf_str})")
    elif status == "staged":
        print(f"\nQueued for review (confidence {conf_str}): {result.get('reason')}")
    else:
        print(f"\nFailed: {result}")
