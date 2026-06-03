"""analysis/food_chat.py — conversational diet-log wrapper over ingest/food.py (V3-5).

Flow: preview_meal() classifies a photo- or text-described meal WITHOUT writing, so the
skill can CONFIRM the parsed rows with the user first (CLAUDE.md/SKILL: confirm before
write); log_meal() then writes via the contract (ingest_food_row → food_logs, staging the
low-confidence/implausible ones); daily_total() keeps a running total vs the calorie
TARGET the skill passes from the context MD (never a hardcoded default).

The vision OCR for a photo is the skill's job (Claude vision) — it passes the resulting
items/description here with method='photo'. This module is calculation + write discipline,
not interpretation; per-person framing (e.g. Dea's growth-not-restriction language) is
applied by the skill from her context safety rules, not here.
"""
from __future__ import annotations

from ingest.food import classify_food, _lookup_guidance, ingest_food_row


def preview_meal(conn, profile_id: str, *, items=None, description=None) -> dict:
    """Classify a meal WITHOUT writing — returns parsed macros + flags for confirmation.

    `items` is a list of food strings (from text or photo OCR); `description` is free text.
    """
    items = items or ([description] if description else [])
    hits, misses = _lookup_guidance(conn, items)
    llm = classify_food(items, hits)
    return {
        "needs_confirmation": True,
        "items": items,
        "guidance_hits": [{"item": h["item"], "classification": h["classification"]} for h in hits],
        "unmatched": [m["item"] for m in misses],
        "parsed": {k: llm.get(k) for k in
                   ("calories", "protein_g", "carbs_g", "fat_g", "fiber_g", "verdict", "flags")},
        "confidence": float(llm.get("confidence", 0.5)),
    }


def log_meal(conn, profile_id: str, *, items=None, description=None, meal_type=None,
             meal_time=None, log_date=None, location=None, notes=None,
             method: str = "manual") -> dict:
    """Write a confirmed meal via the contract. method='photo' when OCR'd from an image.

    Returns the contract result: inserted / staged (low-confidence or implausible
    calories — the comma-slip backstop) / failed. The caller commits.
    """
    payload = {"items": items, "description": description, "meal_type": meal_type,
               "meal_time": meal_time, "log_date": log_date, "location": location,
               "notes": notes}
    payload = {k: v for k, v in payload.items() if v is not None}
    return ingest_food_row(payload, conn, profile_id, method=method)


def daily_total(conn, profile_id: str, *, log_date=None, calorie_target=None) -> dict:
    """Running totals for a day vs the context-MD calorie target (factual; skill frames).

    Excludes day-summary rows so per-meal entries aren't double-counted. ``calorie_target``
    comes from the caller's context MD; when omitted, the comparison fields are None and the
    skill must ask for / read the target rather than assume one.
    """
    cur = conn.cursor()
    cur.execute(
        """SELECT COALESCE(sum(calories),0), COALESCE(sum(protein_g),0), count(*)
           FROM food_logs
           WHERE profile_id=%s
             AND log_date = COALESCE(%s, CURRENT_DATE)
             AND (is_day_summary IS NULL OR is_day_summary = false)""",
        (profile_id, log_date),
    )
    cal, protein, meals = cur.fetchone()
    cal = float(cal); protein = float(protein)
    out = {"log_date": str(log_date) if log_date else "today", "meals": meals,
           "consumed_cal": round(cal), "protein_g": round(protein, 1),
           "target_cal": None, "remaining_cal": None, "pct_of_target": None}
    if calorie_target:
        t = float(calorie_target)
        out["target_cal"] = round(t)
        out["remaining_cal"] = round(t - cal)
        out["pct_of_target"] = round(100.0 * cal / t) if t else None
    return out
