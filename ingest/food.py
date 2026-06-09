"""ingest/food.py — interactive food logging with LLM classification.

Architecture §3.3:
  - Interactive Q&A: ask meal_type, time, location, items.
  - Resolve each item against food_guidance.item for classification.
  - LLM classify verdict + macros via Anthropic Claude API.
  - Write to food_logs (source='manual_skill').
  - Low-confidence → stg_food_log_review.

API seam: ingest_food_row(payload, conn, profile_id) — callable by future RPCs.

Rules enforced:
  #1  No hardcoded thresholds — confidence_min from system_config.
  #2  AI extraction → staging first; only high-confidence to prod.
  #8  validate() before any prod write.
  #9  No hardcoded UUIDs.
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime
from typing import Any

import psycopg2.extras

from lib.contract import (
    confidence_min,
    open_sync_log,
    close_sync_log,
    stage,
    validate,
    write,
    _as_jsonb_param,
)

# ---------------------------------------------------------------------------
# LLM seam — isolated so tests can patch it cleanly
# ---------------------------------------------------------------------------


def classify_food(items: list[str], guidance_hits: list[dict]) -> dict:
    """Call Claude to classify the meal and estimate macros.

    Returns a dict with keys:
        verdict      : "green" | "amber" | "red"
        confidence   : float 0..1
        calories     : int | None
        protein_g    : float | None
        carbs_g      : float | None
        fat_g        : float | None
        fiber_g      : float | None
        flags        : list[str]
        reasoning    : str
    """
    import anthropic  # lazy import so the module loads without the package present

    from lib import models

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    client = anthropic.Anthropic(api_key=api_key)

    guidance_text = ""
    if guidance_hits:
        lines = [
            f"  - {h['item']}: {h['classification']} ({h.get('reason', '')})"
            for h in guidance_hits
        ]
        guidance_text = "\nFood guidance database matches:\n" + "\n".join(lines)

    prompt = f"""You are a clinical nutrition assistant for a metabolic health platform.
Classify this meal and estimate macros.

Meal items: {', '.join(items)}
{guidance_text}

Respond with ONLY a valid JSON object (no markdown, no code fences):
{{
  "verdict": "<green|amber|red>",
  "confidence": <0.0–1.0>,
  "calories": <integer or null>,
  "protein_g": <float or null>,
  "carbs_g": <float or null>,
  "fat_g": <float or null>,
  "fiber_g": <float or null>,
  "flags": ["<tag>", ...],
  "reasoning": "<1 sentence>"
}}

Verdict guidance:
  green  = mostly whole foods, aligned with metabolic health goals
  amber  = mixed — some concerns but acceptable occasionally
  red    = high GI, processed, or conflicts with metabolic health goals

Set confidence lower (< 0.7) when items are vague or portions are unknown."""

    # One model everywhere (models.SONNET); override via HS_FOOD_MODEL env if ever needed. Routed
    # through models.create_message so a retired/invalid id raises a clear error, not a silent 404.
    # (Was hardcoded claude-3-5-haiku-20241022, RETIRED 2026-02-19 — see lib/models.py.)
    model = os.environ.get("HS_FOOD_MODEL") or models.SONNET
    message = models.create_message(
        client,
        model=model,
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )
    if not message.content:
        raise ValueError("Claude returned an empty content list — cannot classify food")
    raw = message.content[0].text.strip()
    # Strip code-fence wrappers the model occasionally emits (```json ... ```)
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip().rstrip("`").strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Claude response was not valid JSON: {raw[:200]}") from exc


# ---------------------------------------------------------------------------
# Food guidance lookup — per item, not per whole meal
# ---------------------------------------------------------------------------


def _lookup_guidance(conn, items: list[str]) -> tuple[list[dict], list[dict]]:
    """Match each item against food_guidance. Returns (hits, misses)."""
    cur = conn.cursor()
    hits, misses = [], []
    for item in items:
        cur.execute(
            """SELECT id, item, classification, reason, alternative
               FROM food_guidance
               WHERE is_active AND lower(item) = lower(%s)
               LIMIT 1""",
            (item,),
        )
        row = cur.fetchone()
        if row:
            hits.append(
                {
                    "item": item,
                    "guidance_id": row[0],
                    "db_item": row[1],
                    "classification": row[2],
                    "reason": row[3],
                    "alternative": row[4],
                }
            )
        else:
            misses.append({"item": item})
    return hits, misses


# ---------------------------------------------------------------------------
# Core API seam — callable by CLI and future RPCs alike
# ---------------------------------------------------------------------------


_VALID_METHODS = {"csv", "screenshot", "photo", "manual", "api"}


def ingest_food_row(payload: dict, conn, profile_id: str,
                    method: str = "manual") -> dict:
    """Ingest one food log entry through the full contract.

    `method` records the ingestion modality on the sync-log run: 'manual' for typed
    entry (default), 'photo' when parsed from a meal photo. Must be one of
    wearable_sync_log's allowed methods.

    payload keys (all optional except profile_id in the call signature):
        meal_type   : str   e.g. "breakfast"
        meal_time   : str   e.g. "08:30" or ISO datetime
        log_date    : str   ISO date (defaults to today)
        location    : str
        items       : list[str]  individual food items
        description : str   free-text description (auto-built from items if absent)
        notes       : str

    Returns {"status": "inserted"|"updated"|"staged"|"failed", ...}
    """
    # -- resolve items list ---------------------------------------------------
    items: list[str] = payload.get("items") or []
    description = payload.get("description") or (", ".join(items) if items else "")
    log_date = payload.get("log_date") or str(date.today())

    # -- lookup guidance for each item ----------------------------------------
    hits, misses = _lookup_guidance(conn, items)

    # -- LLM classification ---------------------------------------------------
    llm_result = classify_food(items or [description], hits)
    confidence: float = float(llm_result.get("confidence", 0.5))

    # -- assemble the production row ------------------------------------------
    foods_value = [
        {"item": h["item"], "classification": h["classification"]}
        for h in hits
    ] + [{"item": m["item"], "classification": "unknown"} for m in misses]

    row: dict[str, Any] = {
        "profile_id": profile_id,
        "log_date": log_date,
        "source": "manual_skill",
        "description": description,
        "meal_type": payload.get("meal_type"),
        "meal_time": payload.get("meal_time"),
        "location": payload.get("location"),
        "verdict": llm_result.get("verdict"),
        "calories": llm_result.get("calories"),
        "protein_g": llm_result.get("protein_g"),
        "carbs_g": llm_result.get("carbs_g"),
        "fat_g": llm_result.get("fat_g"),
        "fiber_g": llm_result.get("fiber_g"),
        # jsonb: encode to string for psycopg2
        "foods": _as_jsonb_param(foods_value) if foods_value else None,
        # text[]: pass as Python list (psycopg2 handles arrays natively)
        "flags": llm_result.get("flags") or [],
        "notes": payload.get("notes"),
    }
    # strip None values (but keep empty lists)
    row = {k: v for k, v in row.items() if v is not None or k == "flags"}

    # -- confidence check (rule #1: read from DB) -----------------------------
    threshold = confidence_min(conn)

    # -- staging merge for LLM output ----------------------------------------
    stg_source = {**payload, **row, "profile_id": profile_id}

    if confidence < threshold:
        stage(conn, "food_logs", stg_source, confidence, raw_text=json.dumps(payload))
        return {
            "status": "staged",
            "reason": "low_confidence",
            "confidence": confidence,
        }

    # -- validate before prod write (rule #8) ---------------------------------
    ok, errors = validate(conn, "food_logs", row)
    if not ok:
        stage(conn, "food_logs", stg_source, confidence, raw_text=json.dumps(payload))
        return {
            "status": "staged",
            "reason": "validation_failed",
            "errors": errors,
            "confidence": confidence,
        }

    # -- manual food has no partial-index unique constraint  ------------------
    # food_logs only has a partial unique index WHERE source_log_path IS NOT NULL.
    # Manual entries (source_log_path=NULL) are not covered by that index, so
    # we cannot use ON CONFLICT on it. We do a plain INSERT instead.
    # This is intentional — duplicate prevention for manual entries is handled
    # at the interactive layer (user-initiated, single entry per session).
    cur = conn.cursor()
    cols = list(row.keys())
    qcols = ", ".join(f'"{c}"' for c in cols)
    placeholders = ", ".join(["%s"] * len(cols))
    sql = f'INSERT INTO "food_logs" ({qcols}) VALUES ({placeholders}) RETURNING id'
    cur.execute(sql, [row[c] for c in cols])
    inserted_id = cur.fetchone()[0]
    return {"status": "inserted", "id": str(inserted_id), "confidence": confidence}


# ---------------------------------------------------------------------------
# Interactive CLI entry point
# ---------------------------------------------------------------------------


def _ask(prompt: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    val = input(f"{prompt}{suffix}: ").strip()
    return val or (default or "")


def run_interactive(conn, profile_id: str) -> None:
    """Prompt the user for a meal and log it."""
    print("\n--- Log a meal ---")
    meal_type = _ask("Meal type (breakfast/lunch/dinner/snack)", "lunch")
    meal_time = _ask("Time (HH:MM or leave blank for now)", datetime.now().strftime("%H:%M"))
    log_date = _ask("Date (YYYY-MM-DD)", str(date.today()))
    location = _ask("Location (optional)", "")
    items_raw = _ask("Food items (comma-separated)", "")
    items = [i.strip() for i in items_raw.split(",") if i.strip()]
    notes = _ask("Notes (optional)", "")

    payload = {
        "meal_type": meal_type,
        "meal_time": meal_time or None,
        "log_date": log_date,
        "location": location or None,
        "items": items,
        "notes": notes or None,
    }

    sync_log_id = open_sync_log(
        conn, provider="manual", method="manual",
        profile_id=profile_id, sync_type="ingest",
    )
    try:
        result = ingest_food_row(payload, conn, profile_id)
        conn.commit()
        status = result["status"]
        conf = result.get("confidence", "?")
        if status in ("inserted", "updated"):
            print(f"\nLogged: {result.get('id')}  (confidence {conf:.0%})")
        elif status == "staged":
            print(f"\nQueued for review (confidence {conf:.0%} — below threshold): {result.get('reason')}")
        else:
            print(f"\nFailed: {result}")
        close_sync_log(
            conn, sync_log_id,
            status="success" if status != "failed" else "failed",
            records_in=1,
            records_upserted=1 if status == "inserted" else 0,
            records_skipped=0,
            records_failed=1 if status == "failed" else 0,
        )
    except Exception as exc:
        conn.rollback()
        close_sync_log(conn, sync_log_id, status="failed", records_in=1, records_failed=1)
        print(f"\nError: {exc}")
        raise
