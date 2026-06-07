"""
inbox_drain — Phase 3A cloud drain for media_inbox.

Fetch settled pending items, cluster by media_group_id, write via
SECURITY DEFINER RPCs, mark done/staged.

Designed for cloud Routine environments: uses lib.db_rest (httpx only).
The Routine's LLM handles the extraction step; this module handles
all DB I/O before and after.

Cluster lifecycle:
  1. fetch_settled()    — get items older than settle_sec
  2. claim()            — atomic PATCH (pending → processing); skip if lost
  3. build_clusters()   — split into album clusters + ungrouped singletons
  4. <Routine LLM>      — vision-extract each cluster; content-cluster ungrouped
  5. write_food/biomarker/supplement() — validate via RPC, mark rows done/staged
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from lib.db_rest import DbRest

log = logging.getLogger(__name__)

_SETTLE_SEC_FALLBACK = 90
_CONFIDENCE_FALLBACK = 0.7


# ── config ────────────────────────────────────────────────────────────────────

def get_config(db: DbRest) -> dict[str, Any]:
    """Read all system_config rows as {key: native_value}."""
    rows = db.select("system_config", select="key,value", filters={"is_active": "eq.true"})
    return {r["key"]: r["value"] for r in rows}


# ── fetch + claim ─────────────────────────────────────────────────────────────

def fetch_settled(
    db: DbRest,
    settle_sec: int,
    chat_id: int | None = None,
) -> list[dict]:
    """Return pending media_inbox rows older than settle_sec seconds.

    Older-first order so albums arrive fully before the drain picks them up.
    Optionally filtered to a single chat_id.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=settle_sec)).isoformat()
    filters: dict[str, str] = {
        "status": "eq.pending",
        "created_at": f"lt.{cutoff}",
    }
    if chat_id is not None:
        filters["chat_id"] = f"eq.{chat_id}"
    return db.select("media_inbox", filters=filters, order="created_at")


def claim(db: DbRest, item_id: str) -> bool:
    """Atomically transition one media_inbox item pending → processing.

    Returns True if this call claimed it; False if another Routine fire
    already claimed it (idempotency latch).
    """
    return db.claim_inbox_item(item_id)


# ── clustering ────────────────────────────────────────────────────────────────

def build_clusters(items: list[dict]) -> tuple[list[list[dict]], list[dict]]:
    """Split items into deterministic album clusters and ungrouped singletons.

    Returns:
        album_clusters — list of lists; each inner list shares a media_group_id
                         and contains ≥ 2 rows (i.e. confirmed album bursts).
        ungrouped      — rows with no media_group_id, plus media_group_id
                         singletons (only one photo arrived; treat separately).

    The Routine LLM handles further content-based clustering of ungrouped items
    (e.g. front + back of the same label, or two unrelated foods).
    """
    album_map: dict[str, list[dict]] = defaultdict(list)
    no_group: list[dict] = []

    for item in items:
        mgid = item.get("media_group_id")
        if mgid:
            album_map[mgid].append(item)
        else:
            no_group.append(item)

    album_clusters = [rows for rows in album_map.values() if len(rows) >= 2]
    singletons = [rows[0] for rows in album_map.values() if len(rows) == 1]

    return album_clusters, no_group + singletons


def merge_caption(rows: list[dict]) -> str:
    """Merge distinct non-empty captions from a cluster with ' | ' separator."""
    seen: set[str] = set()
    parts: list[str] = []
    for r in rows:
        cap = (r.get("caption") or "").strip()
        if cap and cap not in seen:
            seen.add(cap)
            parts.append(cap)
    return " | ".join(parts)


# ── write ─────────────────────────────────────────────────────────────────────

def _ids(rows: list[dict]) -> list[str]:
    return [str(r["id"]) for r in rows]


def mark_rows(
    db: DbRest,
    row_ids: list[str],
    status: str,
    result_ref: str | None = None,
) -> None:
    """Bulk-update media_inbox status for all rows in a cluster.

    status values: 'done' (written to prod), 'staged' (written to review),
                   'failed' (RPC error). Atomic: all-or-nothing per cluster.
    """
    if not row_ids:
        return
    body: dict[str, Any] = {
        "status": status,
        "processed_at": datetime.now(timezone.utc).isoformat(),
    }
    if result_ref is not None:
        body["result_ref"] = result_ref
    db.update(
        "media_inbox",
        {"id": f"in.({','.join(row_ids)})"},
        body,
    )


def _write(
    db: DbRest,
    rows: list[dict],
    rpc_name: str,
    rpc_args: dict,
) -> dict:
    """Call a SECURITY DEFINER RPC, mark rows, return {id, status}."""
    try:
        result = db.rpc(rpc_name, rpc_args) or {}
    except Exception as exc:
        log.error("RPC %s failed: %s", rpc_name, exc)
        mark_rows(db, _ids(rows), "failed")
        return {}
    rpc_status = result.get("status", "")
    media_status = "done" if rpc_status == "inserted" else "staged" if rpc_status == "staged" else "failed"
    mark_rows(db, _ids(rows), media_status, str(result.get("id", "")) or None)
    return result


def write_food(
    db: DbRest,
    rows: list[dict],
    profile_id: str,
    extracted: dict[str, Any],
    confidence: float,
    raw_text: str,
) -> dict:
    """Write food extraction to food_logs (or stg_food_log_review if low-confidence).

    extracted keys: meal_type, description, calories, protein_g, carbs_g,
                    fat_g, fiber_g, logged_at (ISO), notes, foods (JSON).
    """
    return _write(db, rows, "maintainer_ingest_food", {
        "p_profile_id":  profile_id,
        "p_meal_type":   extracted.get("meal_type"),
        "p_description": extracted.get("description"),
        "p_calories":    extracted.get("calories"),
        "p_protein_g":   extracted.get("protein_g"),
        "p_carbs_g":     extracted.get("carbs_g"),
        "p_fat_g":       extracted.get("fat_g"),
        "p_fiber_g":     extracted.get("fiber_g"),
        "p_logged_at":   extracted.get("logged_at"),
        "p_source":      "telegram",
        "p_notes":       extracted.get("notes"),
        "p_foods":       extracted.get("foods"),
        "p_confidence":  confidence,
        "p_raw_text":    raw_text,
    })


def write_biomarker(
    db: DbRest,
    rows: list[dict],
    profile_id: str,
    extracted: dict[str, Any],
    confidence: float,
    raw_text: str,
) -> dict:
    """Write biomarker extraction to biomarkers (or stg_biomarker_review).

    extracted keys: metric_definition_id, value, unit, measured_at (ISO),
                    notes, extracted_name (human-readable name from OCR).
    """
    return _write(db, rows, "maintainer_ingest_biomarker", {
        "p_profile_id":            profile_id,
        "p_metric_definition_id":  extracted.get("metric_definition_id"),
        "p_value":                 extracted["value"],
        "p_unit":                  extracted.get("unit"),
        "p_measured_at":           extracted.get("measured_at"),
        "p_source":                "telegram",
        "p_notes":                 extracted.get("notes"),
        "p_extracted_name":        extracted.get("extracted_name"),
        "p_confidence":            confidence,
        "p_raw_text":              raw_text,
    })


def write_supplement(
    db: DbRest,
    rows: list[dict],
    profile_id: str,
    extracted: dict[str, Any],
    confidence: float,
    raw_text: str,
) -> dict:
    """Write supplement intake to supplement_intake_logs (or stg_supplement_intake_review).

    extracted keys: supplement_id, dose_amount, dose_unit, taken_at (ISO),
                    notes, name (extracted text name for staging).
    """
    return _write(db, rows, "maintainer_ingest_supplement", {
        "p_profile_id":     profile_id,
        "p_supplement_id":  extracted.get("supplement_id"),
        "p_dose_amount":    extracted.get("dose_amount"),
        "p_dose_unit":      extracted.get("dose_unit"),
        "p_taken_at":       extracted.get("taken_at"),
        "p_source":         "telegram",
        "p_notes":          extracted.get("notes"),
        "p_extracted_name": extracted.get("name"),
        "p_confidence":     confidence,
        "p_raw_text":       raw_text,
    })


# ── lookups (for Routine LLM to resolve names → UUIDs) ───────────────────────

def lookup_metric(db: DbRest, query: str) -> list[dict]:
    """Search metric_definitions by name (ilike). Returns id, name, display_name, unit."""
    return db.select(
        "metric_definitions",
        select="id,name,display_name,unit",
        filters={"name": f"ilike.*{query}*", "is_active": "eq.true"},
        limit=5,
    )


def lookup_supplement_by_name(db: DbRest, query: str) -> list[dict]:
    """Search supplements by name (ilike). Returns id, name, display_name, default_unit."""
    return db.select(
        "supplements",
        select="id,name,display_name,default_unit",
        filters={"name": f"ilike.*{query}*", "is_active": "eq.true"},
        limit=5,
    )


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import json
    import os
    import sys

    from lib.db_rest import sign_in

    url = os.environ["SUPABASE_URL"]
    anon = os.environ["SUPABASE_ANON_KEY"]
    email = os.environ["HS_AUTH_EMAIL"]
    pwd = os.environ["HS_AUTH_PASSWORD"]

    jwt = sign_in(url, anon, email, pwd)
    with DbRest(url, anon, jwt) as db:
        cfg = get_config(db)
        settle = int(cfg.get("push.inbox_settle_sec", _SETTLE_SEC_FALLBACK))
        items = fetch_settled(db, settle)
        albums, ungrouped = build_clusters(items)
        print(f"Settled: {len(items)} items | {len(albums)} album clusters | {len(ungrouped)} ungrouped")
        for cluster in albums:
            cap = merge_caption(cluster)
            ids = _ids(cluster)
            print(f"  Album {cluster[0].get('media_group_id')}: {len(cluster)} rows, caption={cap!r}")
        for item in ungrouped:
            print(f"  Ungrouped id={item['id']} kind={item.get('kind')} caption={str(item.get('caption',''))[:60]!r}")
