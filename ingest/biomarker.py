"""ingest/biomarker.py — biomarker ingestion against metric_definitions.

Architecture §3.4:
  - Resolve marker name via metric_definitions.name (exact then substring).
  - Fallback: resolve via loinc_reference.loinc_code → metric_definitions.loinc_id.
  - Range-check value vs metric_definitions.min_value / max_value.
  - Write to biomarkers ON CONFLICT (profile_id, metric_definition_id, measured_at).
  - Low-confidence or out-of-range → stg_biomarker_review.

All routing is delegated to contract.ingest_record — this module adds only the
loinc fallback before the contract sees the payload.

Rules enforced:
  #1  No hardcoded thresholds.
  #2  AI extraction → staging first.
  #6  Column names verified against information_schema.
  #8  validate() is inside ingest_record — never bypassed.
  #9  No hardcoded UUIDs.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from lib.contract import (
    confidence_min,
    ingest_record,
    open_sync_log,
    close_sync_log,
)

# Production unique constraint (migration 008a):
#   UNIQUE(profile_id, metric_definition_id, measured_at)
_CONFLICT_COLS = ["profile_id", "metric_definition_id", "measured_at"]


# ---------------------------------------------------------------------------
# LOINC fallback resolver
# ---------------------------------------------------------------------------


def _resolve_via_loinc(conn, loinc_code: str) -> "str | None":
    """Given a LOINC code string, return the matching metric_definition_id or None.

    loinc_reference.loinc_code (text) → metric_definitions.loinc_id (uuid FK).
    """
    cur = conn.cursor()
    cur.execute(
        """SELECT md.id
           FROM loinc_reference lr
           JOIN metric_definitions md ON md.loinc_id = lr.id
           WHERE lr.loinc_code = %s AND md.is_active
           LIMIT 1""",
        (loinc_code,),
    )
    row = cur.fetchone()
    return str(row[0]) if row else None


# ---------------------------------------------------------------------------
# Public ingest function
# ---------------------------------------------------------------------------


def ingest_biomarker_row(payload: dict, conn, profile_id: str) -> dict:
    """Ingest one biomarker through the full contract pipeline.

    payload keys:
        name / metric : str   metric name or display_name
        loinc_code    : str   LOINC code (fallback if name doesn't resolve)
        value         : float
        unit          : str
        measured_at   : str   ISO datetime (required for prod write)
        source        : str   defaults to 'manual'
        lab_name      : str   optional

    Returns {"status": "inserted"|"updated"|"staged"|"failed", ...}
    """
    raw: dict[str, Any] = {
        "profile_id": profile_id,
        "name": payload.get("name") or payload.get("metric"),
        "value": payload.get("value"),
        "unit": payload.get("unit"),
        "measured_at": payload.get("measured_at"),
        "source": payload.get("source", "manual"),
        "lab_name": payload.get("lab_name"),
        **{k: v for k, v in payload.items()
           if k not in ("name", "metric", "value", "unit", "measured_at", "source", "lab_name")},
    }

    # -- LOINC fallback: if the name won't resolve, try loinc_code -----------
    loinc_code = payload.get("loinc_code")
    if loinc_code and not raw.get("name"):
        metric_def_id = _resolve_via_loinc(conn, loinc_code)
        if metric_def_id:
            # Inject pre-resolved FK; contract.resolve("biomarker") will exact-match
            # the metric_definition name, but we short-circuit by providing the id.
            # We set name="" so the resolver's exact/substring search misses,
            # then fix metric_definition_id directly in normalised if needed.
            # Simpler: inject metric_definition_id and a dummy name matching it.
            cur = conn.cursor()
            cur.execute(
                "SELECT name FROM metric_definitions WHERE id = %s", (metric_def_id,)
            )
            md_row = cur.fetchone()
            if md_row:
                raw["name"] = md_row[0]

    sync_log_id = open_sync_log(
        conn, provider="manual", method="biomarker_ingest",
        profile_id=profile_id, sync_type="ingest",
    )
    try:
        result = ingest_record(
            conn,
            sync_log_id=sync_log_id,
            kind="biomarker",
            table="biomarkers",
            raw=raw,
            conflict_cols=_CONFLICT_COLS,
        )
        return result
    finally:
        # caller owns commit; we just close the log
        status = "success"
        close_sync_log(
            conn, sync_log_id,
            status=status,
            records_in=1,
            records_upserted=1 if result.get("status") in ("inserted", "updated") else 0,
            records_skipped=1 if result.get("status") == "skipped" else 0,
            records_failed=1 if result.get("status") == "failed" else 0,
        )


# ---------------------------------------------------------------------------
# Interactive CLI entry point
# ---------------------------------------------------------------------------


def _ask(prompt: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    val = input(f"{prompt}{suffix}: ").strip()
    return val or (default or "")


def run_interactive(conn, profile_id: str) -> None:
    """Prompt the user for a biomarker value and log it."""
    print("\n--- Log a biomarker ---")
    name = _ask("Marker name (e.g. hba1c, fasting_glucose)")
    value_str = _ask("Value")
    unit = _ask("Unit (leave blank to use catalog default)", "")
    measured_at = _ask(
        "Measured at (ISO datetime, e.g. 2026-06-01T09:00:00+05:30)",
        datetime.now(timezone.utc).isoformat(),
    )
    source = _ask("Source", "manual")
    lab_name = _ask("Lab name (optional)", "")

    payload = {
        "name": name,
        "value": float(value_str),
        "unit": unit or None,
        "measured_at": measured_at,
        "source": source,
        "lab_name": lab_name or None,
    }

    result = ingest_biomarker_row(payload, conn, profile_id)
    conn.commit()

    status = result["status"]
    conf = result.get("confidence", "?")
    if isinstance(conf, float):
        conf_str = f"{conf:.0%}"
    else:
        conf_str = str(conf)

    if status in ("inserted", "updated"):
        print(f"\nWritten to biomarkers  (confidence {conf_str})")
    elif status == "staged":
        print(f"\nQueued for review (confidence {conf_str}): {result.get('reason')}")
    else:
        print(f"\nFailed: {result}")
