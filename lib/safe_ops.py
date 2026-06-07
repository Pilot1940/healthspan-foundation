"""lib/safe_ops.py — safe utilities for correction / manual operations.

These guards exist because a date-only bulk UPDATE can silently sweep rows
the caller didn't own (e.g. all supplements sharing a taken_on date instead
of the specific rows that were touched).

Rules enforced here:
  1. Updates must target an explicit id list — never a bare date predicate.
  2. Dry-run is the default — callers must opt into mutation explicitly.
  3. Day reassignments preserve the original timestamp in notes for recovery.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

# Tables that support day reassignment and their timestamp/date column names.
# supplement_intake_logs.taken_on is GENERATED from taken_at — do not write it directly.
_REASSIGN_TABLES: dict[str, dict[str, Any]] = {
    "food_logs": {
        "ts_col":   "logged_at",
        "date_col": "log_date",  # regular DATE column; must be updated explicitly
    },
    "supplement_intake_logs": {
        "ts_col":   "taken_at",
        "date_col": None,  # taken_on is GENERATED ALWAYS AS ((taken_at AT TIME ZONE 'UTC')::date)
    },
}


def today_utc() -> str:
    """Today's date in UTC as YYYY-MM-DD.

    Always UTC — same clock source as the production drain pipeline.
    Use this instead of date.today() to avoid host-clock timezone skew
    in cloud Routines where the system clock may lag or have no TZ set.
    """
    return datetime.now(timezone.utc).date().isoformat()


def safe_bulk_update(
    conn,
    table: str,
    *,
    ids: list[str],
    update_data: dict[str, Any],
    dry_run: bool = True,
) -> list[dict]:
    """UPDATE rows in `table` identified by an explicit id list.

    Never accepts a date predicate — always targets by primary key.
    Dry-run by default: returns affected rows without mutating.

    Returns:
        List of dicts; each row gets an '_action' key:
          'would_update' (dry_run=True) or 'updated' (dry_run=False).

    Raises:
        ValueError  — if ids is empty (guards against accidental full-table writes).
    """
    if not ids:
        raise ValueError(
            "safe_bulk_update: ids must be non-empty. "
            "Never update by a date predicate — always identify rows by primary key first."
        )
    if not update_data:
        raise ValueError("safe_bulk_update: update_data must be non-empty.")

    def _fetch(cur) -> list[dict]:
        cur.execute(f"SELECT * FROM {table} WHERE id = ANY(%s)", (ids,))  # noqa: S608
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    with conn.cursor() as cur:
        rows = _fetch(cur)

    if dry_run:
        for row in rows:
            row["_action"] = "would_update"
            row["_pending"] = update_data
        return rows

    set_clause = ", ".join(f"{k} = %s" for k in update_data)
    params = list(update_data.values()) + [ids]
    with conn.cursor() as cur:
        cur.execute(f"UPDATE {table} SET {set_clause} WHERE id = ANY(%s)", params)  # noqa: S608
    conn.commit()

    with conn.cursor() as cur:
        rows = _fetch(cur)
    for row in rows:
        row["_action"] = "updated"
    return rows


def reassign_day_by_ids(
    conn,
    table: str,
    *,
    ids: list[str],
    new_taken_at: str | datetime,
    dry_run: bool = True,
) -> list[dict]:
    """Move log entries to a different day. Requires an explicit id list.

    Supported tables:
      'food_logs'               — updates logged_at + log_date (both are regular columns).
      'supplement_intake_logs'  — updates taken_at; taken_on (GENERATED) auto-updates.

    Original timestamp is preserved in the row's notes field for recovery:
      "[prior logged_at: 2026-06-05T08:30:00+00:00]"

    Returns:
        List of dicts describing what was (or would be) changed.

    Raises:
        ValueError — if ids is empty or table is not supported.
    """
    if table not in _REASSIGN_TABLES:
        raise ValueError(
            f"reassign_day_by_ids: table '{table}' not supported. "
            f"Must be one of: {sorted(_REASSIGN_TABLES)}"
        )
    if not ids:
        raise ValueError(
            "reassign_day_by_ids: ids must be non-empty. "
            "Never reassign by date predicate — that sweeps all rows sharing a date."
        )

    meta = _REASSIGN_TABLES[table]
    ts_col   = meta["ts_col"]
    date_col = meta["date_col"]

    if isinstance(new_taken_at, str):
        dt = datetime.fromisoformat(new_taken_at)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = new_taken_at
    new_date = dt.date().isoformat()

    with conn.cursor() as cur:
        cur.execute(
            f"SELECT id, {ts_col}, notes FROM {table} WHERE id = ANY(%s)",  # noqa: S608
            (ids,),
        )
        rows = [{"id": str(row[0]), ts_col: row[1], "notes": row[2]} for row in cur.fetchall()]

    if dry_run:
        return [
            {
                "id":            row["id"],
                "action":        "would_reassign",
                f"old_{ts_col}": str(row[ts_col]),
                f"new_{ts_col}": dt.isoformat(),
                "new_date":      new_date if date_col else "(generated from taken_at)",
                "notes_preview": f"{row['notes'] or ''}\n[prior {ts_col}: {row[ts_col]}]".strip(),
            }
            for row in rows
        ]

    results = []
    for row in rows:
        original_ts = row[ts_col]
        prior_note  = f"[prior {ts_col}: {original_ts}]"
        new_notes   = f"{row['notes'] or ''}\n{prior_note}".strip()

        with conn.cursor() as cur:
            if date_col:
                cur.execute(
                    f"UPDATE {table} SET {ts_col} = %s, {date_col} = %s, notes = %s WHERE id = %s",  # noqa: S608
                    (dt, new_date, new_notes, row["id"]),
                )
            else:
                cur.execute(
                    f"UPDATE {table} SET {ts_col} = %s, notes = %s WHERE id = %s",  # noqa: S608
                    (dt, new_notes, row["id"]),
                )

        results.append({
            "id":            row["id"],
            "action":        "reassigned",
            f"old_{ts_col}": str(original_ts),
            f"new_{ts_col}": dt.isoformat(),
            "new_date":      new_date if date_col else "(auto from taken_at)",
        })

    conn.commit()
    return results
