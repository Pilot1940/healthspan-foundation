"""
inbox_drain — autonomous drain with built-in vision extraction.

Run:  python -m monitor.inbox_drain --once
Env:  SUPABASE_URL, SUPABASE_ANON_KEY, HS_AUTH_EMAIL, HS_AUTH_PASSWORD,
      HS_ANTHROPIC_API_KEY, TELEGRAM_BOT_TOKEN

The --once flag runs the full loop non-interactively:
  fetch settled pending → atomic claim → download images from Storage →
  vision extract via Anthropic API → cluster → write via maintainer_ingest_* RPC →
  mark done/staged → send Telegram confirmation → print JSON summary.

No agent reasoning, no interactive steps.
"""
from __future__ import annotations

import base64
import json
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from lib.db_rest import DbRest

log = logging.getLogger(__name__)

_SETTLE_SEC_FALLBACK = 90
_CONFIDENCE_FALLBACK = 0.7
_VISION_MODEL_FALLBACK = "claude-sonnet-4-6"
_ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
_ANTHROPIC_VERSION = "2023-06-01"


# ── config ────────────────────────────────────────────────────────────────────

def get_config(db: DbRest) -> dict[str, Any]:
    """Read all active system_config rows as {key: value}."""
    rows = db.select("system_config", select="key,value", filters={"is_active": "eq.true"})
    return {r["key"]: r["value"] for r in rows}


# ── fetch + claim ─────────────────────────────────────────────────────────────

def fetch_settled(
    db: DbRest,
    settle_sec: int,
    chat_id: int | None = None,
) -> list[dict]:
    """Return pending media_inbox rows older than settle_sec seconds, oldest first."""
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

    Returns True only if this call won; False means another drain already claimed it.
    """
    return db.claim_inbox_item(item_id)


# ── clustering ────────────────────────────────────────────────────────────────

def build_clusters(items: list[dict]) -> tuple[list[list[dict]], list[dict]]:
    """Split items into album clusters (shared media_group_id ≥ 2) and ungrouped."""
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
    stage_reason: str | None = None,
) -> None:
    """Bulk-update media_inbox status for a cluster."""
    if not row_ids:
        return
    body: dict[str, Any] = {
        "status": status,
        "processed_at": datetime.now(timezone.utc).isoformat(),
    }
    if result_ref is not None:
        body["result_ref"] = result_ref
    if stage_reason is not None:
        body["stage_reason"] = stage_reason
    db.update("media_inbox", {"id": f"in.({','.join(row_ids)})"}, body)


def _write(db: DbRest, rows: list[dict], rpc_name: str, rpc_args: dict) -> dict:
    """Call a SECURITY DEFINER RPC, mark rows, return {id, status}."""
    try:
        result = db.rpc(rpc_name, rpc_args) or {}
    except Exception as exc:
        log.error("RPC %s failed: %s", rpc_name, exc)
        mark_rows(db, _ids(rows), "failed", stage_reason=str(exc))
        return {}
    rpc_status = result.get("status", "")
    media_status = (
        "done" if rpc_status == "inserted"
        else "staged" if rpc_status == "staged"
        else "failed"
    )
    stage_reason = result.get("stage_reason") or None
    mark_rows(db, _ids(rows), media_status, str(result.get("id", "")) or None, stage_reason)
    return result


def food_is_complete(extracted: dict) -> bool:
    """True when extraction has enough data to auto-write without review.

    Requires description, calories (non-null), and at least one macro.
    Plausibility bounds on calories are checked DB-side (food_energy_kcal in metric_definitions).
    """
    if not extracted.get("description"):
        return False
    cals = extracted.get("calories")
    if cals is None:
        return False
    try:
        float(cals)
    except (TypeError, ValueError):
        return False
    return any(extracted.get(m) is not None for m in ("protein_g", "carbs_g", "fat_g"))


def supplement_is_complete(extracted: dict) -> bool:
    """True when the supplement resolves to a known id — safe to auto-write.

    Dose is OPTIONAL. Many real supplements have no measured dose (nutritional yeast,
    oral lozenges — their regimen rows carry no dose_amount), and "I took it" is a valid
    adherence record without one (dose_amount/dose_unit are nullable). Requiring a dose
    stranded those in review every time. Dose still defaults from the regimen when the
    regimen has one (see lookup_regimen_dose) for richer data."""
    return bool(extracted.get("supplement_id"))


def biomarker_is_complete(extracted: dict) -> bool:
    """True when biomarker has a resolved metric ID, numeric value, and unit."""
    return bool(
        extracted.get("metric_definition_id")
        and extracted.get("value") is not None
        and extracted.get("unit")
    )


def _food_rpc_args(
    profile_id: str,
    extracted: dict[str, Any],
    confidence: float,
    raw_text: str,
    force_stage: bool,
    stage_reason: str | None = None,
) -> dict:
    return {
        "p_profile_id":    profile_id,
        "p_meal_type":     extracted.get("meal_type"),
        "p_description":   extracted.get("description"),
        "p_calories":      extracted.get("calories"),
        "p_protein_g":     extracted.get("protein_g"),
        "p_carbs_g":       extracted.get("carbs_g"),
        "p_fat_g":         extracted.get("fat_g"),
        "p_fiber_g":       extracted.get("fiber_g"),
        "p_logged_at":     extracted.get("logged_at"),
        "p_source":        "telegram",
        "p_notes":         extracted.get("notes"),
        "p_foods":         extracted.get("foods"),
        "p_confidence":    confidence,
        "p_raw_text":      raw_text,
        "p_force_stage":   force_stage,
        "p_stage_reason":  stage_reason,
    }


def write_food(
    db: DbRest, rows: list[dict], profile_id: str,
    extracted: dict[str, Any], confidence: float, raw_text: str,
    force_stage: bool = False,
    stage_reason: str | None = None,
) -> dict:
    return _write(db, rows, "maintainer_ingest_food",
                  _food_rpc_args(profile_id, extracted, confidence, raw_text,
                                 force_stage, stage_reason))


def _biomarker_rpc_args(
    profile_id: str,
    extracted: dict[str, Any],
    confidence: float,
    raw_text: str,
    force_stage: bool,
    stage_reason: str | None = None,
) -> dict:
    return {
        "p_profile_id":            profile_id,
        "p_metric_definition_id":  extracted.get("metric_definition_id"),
        "p_value":                 extracted.get("value"),
        "p_unit":                  extracted.get("unit"),
        "p_measured_at":           extracted.get("measured_at"),
        "p_source":                "telegram",
        "p_notes":                 extracted.get("notes"),
        "p_extracted_name":        extracted.get("extracted_name"),
        "p_confidence":            confidence,
        "p_raw_text":              raw_text,
        "p_force_stage":           force_stage,
        "p_stage_reason":          stage_reason,
    }


def write_biomarker(
    db: DbRest, rows: list[dict], profile_id: str,
    extracted: dict[str, Any], confidence: float, raw_text: str,
    force_stage: bool = False,
    stage_reason: str | None = None,
) -> dict:
    return _write(db, rows, "maintainer_ingest_biomarker",
                  _biomarker_rpc_args(profile_id, extracted, confidence, raw_text,
                                      force_stage, stage_reason))


def _supplement_rpc_args(
    profile_id: str,
    extracted: dict[str, Any],
    confidence: float,
    raw_text: str,
    force_stage: bool,
    stage_reason: str | None = None,
) -> dict:
    return {
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
        "p_force_stage":    force_stage,
        "p_stage_reason":   stage_reason,
    }


# NOTE: supplements are written inline in _process_cluster's multi-item loop (one or
# many per message), calling maintainer_ingest_supplement via db.rpc directly so the
# cluster rows are marked once by aggregate status. No write_supplement wrapper needed.


# ── lookups ───────────────────────────────────────────────────────────────────

def lookup_metric(db: DbRest, query: str) -> list[dict]:
    return db.select(
        "metric_definitions",
        select="id,name,display_name,unit",
        filters={"name": f"ilike.*{query}*", "is_active": "eq.true"},
        limit=5,
    )


def lookup_supplement_by_name(db: DbRest, query: str) -> list[dict]:
    """Resolve a supplement by name. The LLM returns human display names
    ("Magnesium Citrate", "Vitamin D3"), so match display_name first, then fall back
    to the snake_case internal name."""
    q = (query or "").strip()
    if not q:
        return []
    rows = db.select(
        "supplements",
        select="id,name,display_name,default_unit",
        filters={"display_name": f"ilike.*{q}*", "is_active": "eq.true"},
        limit=5,
    )
    if rows:
        return rows
    return db.select(
        "supplements",
        select="id,name,display_name,default_unit",
        filters={"name": f"ilike.*{q}*", "is_active": "eq.true"},
        limit=5,
    )


def lookup_regimen_dose(db: DbRest, profile_id: str, supplement_id: str) -> dict | None:
    """Return {dose_amount, dose_unit} from the user's regimen for this supplement, so a
    bare "took my magnesium citrate" defaults to the prescribed dose instead of staging.
    Prefers an active regimen row; falls back to any row that carries a dose."""
    rows = db.select(
        "supplement_regimens",
        select="dose_amount,dose_unit,status",
        filters={"profile_id": f"eq.{profile_id}", "supplement_id": f"eq.{supplement_id}"},
    ) or []
    active = [r for r in rows if (r.get("status") in (None, "active"))
              and r.get("dose_amount") is not None and r.get("dose_unit")]
    pool = active or [r for r in rows if r.get("dose_amount") is not None and r.get("dose_unit")]
    if pool:
        return {"dose_amount": pool[0]["dose_amount"], "dose_unit": pool[0]["dose_unit"]}
    return None


def _food_reference_candidates(caption: str, food_item: dict) -> list[str]:
    """Build ordered candidate list for food_reference lookup.
    Caption first (user-typed brand name is most reliable), then vision description,
    then individual ingredient names from foods[].
    """
    seen: set[str] = set()
    result: list[str] = []
    raw_candidates = (
        [caption or ""]
        + [food_item.get("description") or ""]
        + [f.get("name") or "" for f in food_item.get("foods") or []]
    )
    for s in raw_candidates:
        s = s.strip()
        if s and s.lower() not in seen:
            seen.add(s.lower())
            result.append(s)
    return result


def lookup_food_reference(db: DbRest, candidates: list[str], profile_id: str) -> dict | None:
    """Try each candidate name against the food_reference table via RPC.
    Personal entries (matching profile_id) take priority over global ones — enforced in SQL.
    Returns the first matching row or None.
    """
    for name in candidates:
        if not name:
            continue
        try:
            rows = db.rpc("lookup_food_reference", {"p_name": name, "p_profile_id": profile_id}) or []
            if not isinstance(rows, list):
                rows = [rows] if isinstance(rows, dict) and rows.get("name") else []
            for row in rows:
                if isinstance(row, dict) and row.get("name") and row.get("calories") is not None:
                    return row
        except Exception:
            return None
    return None


def lookup_viome_verdicts(db: DbRest, candidates: list[str], profile_id: str) -> list[dict]:
    """Check effective_food_guidance for all candidate names for this profile.
    Returns a list of {item, effective_classification, reason} for avoid/minimize/superfood.
    Uses a SECURITY DEFINER RPC so the drainer account fetches per-profile guidance correctly.
    """
    items = [c.strip() for c in candidates if c and c.strip()]
    if not items:
        return []
    try:
        rows = db.rpc("lookup_viome_verdicts", {"p_items": items, "p_profile_id": profile_id}) or []
        return rows if isinstance(rows, list) else []
    except Exception:
        return []


def lookup_past_food_macros(
    db: DbRest, description: str, profile_id: str, min_logs: int
) -> dict | None:
    """Check food_logs for past confirmed entries matching this description.
    Returns a macro summary dict if ≥ min_logs found; None otherwise.
    min_logs comes from system_config 'food_reference.learn_min_logs' (Rule #1).
    """
    if not description:
        return None
    try:
        rows = db.select(
            "food_logs",
            select="calories,protein_g,carbs_g,fat_g",
            filters={
                "profile_id": f"eq.{profile_id}",
                "description": f"ilike.{description}",
                "calories": "not.is.null",
            },
            limit=10,
        )
        if not rows or len(rows) < min_logs:
            return None
        count = len(rows)
        return {
            "calories": round(sum(float(r.get("calories") or 0) for r in rows) / count),
            "protein_g": round(sum(float(r.get("protein_g") or 0) for r in rows) / count, 1),
            "carbs_g":   round(sum(float(r.get("carbs_g")   or 0) for r in rows) / count, 1),
            "fat_g":     round(sum(float(r.get("fat_g")     or 0) for r in rows) / count, 1),
            "count": count,
        }
    except Exception:
        return None


def _viome_verdict_lines(verdicts: list[dict]) -> str:
    """Compose Viome cross-check lines for the Telegram confirmation (adult only).

    Each line cites the specific offending ingredient so the user knows what to avoid.
    """
    parts: list[str] = []
    for v in verdicts:
        item = (v.get("item") or "").strip()
        cls  = v.get("effective_classification") or ""
        reason = (v.get("reason") or "").strip()
        suffix = f": {reason}" if reason else "."
        if cls == "avoid":
            parts.append(f"⚠️ {item} — AVOID{suffix}")
        elif cls == "minimize":
            parts.append(f"⚠️ {item} — minimize{suffix}")
        elif cls == "superfood":
            parts.append(f"✅ {item} is a superfood for you{suffix}")
    return "\n".join(parts)


def compute_meal_verdict(verdicts: list[dict]) -> tuple[str, list[str]]:
    """Compute a worst-case meal verdict from viome lookup results.

    Precedence: avoid > minimize > superfood > clean.
    Returns (verdict_str, flagged_items_list).

    'avoid'    — any ingredient is on the avoid list
    'minimize' — at least one ingredient should be minimized (no avoids)
    'superfood'— at least one superfood present, no avoid/minimize
    'clean'    — no flagged items
    """
    avoid_items: list[str] = []
    minimize_items: list[str] = []
    superfood_items: list[str] = []

    for v in verdicts:
        item = (v.get("item") or "").strip()
        cls = v.get("effective_classification") or ""
        if not item:
            continue
        if cls == "avoid":
            avoid_items.append(item)
        elif cls == "minimize":
            minimize_items.append(item)
        elif cls == "superfood":
            superfood_items.append(item)

    if avoid_items:
        return "avoid", avoid_items + minimize_items
    if minimize_items:
        return "minimize", minimize_items
    if superfood_items:
        return "superfood", superfood_items
    return "clean", []


# ── storage ───────────────────────────────────────────────────────────────────

def get_signed_url(db: DbRest, storage_path: str, expires_in: int = 300) -> str | None:
    """Create a signed URL for a private health-media Storage object."""
    try:
        resp = db._client.post(
            f"{db._base}/storage/v1/object/sign/health-media/{storage_path}",
            json={"expiresIn": expires_in},
            headers=db._h(),
        )
        resp.raise_for_status()
        signed = resp.json().get("signedURL")
        if signed:
            return db._base + "/storage/v1" + signed if signed.startswith("/") else signed
        return None
    except Exception:
        return None


def _download_image(url: str) -> tuple[bytes, str] | None:
    """Download image bytes from a signed URL. Returns (bytes, content_type) or None."""
    try:
        r = httpx.get(url, timeout=30.0, follow_redirects=True)
        r.raise_for_status()
        ct = r.headers.get("content-type", "image/jpeg").split(";")[0].strip()
        return r.content, ct
    except Exception:
        return None


def _image_block(data: bytes, content_type: str) -> dict:
    """Build an Anthropic vision content block from raw image bytes."""
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": content_type,
            "data": base64.b64encode(data).decode(),
        },
    }


# ── vision ────────────────────────────────────────────────────────────────────

_VISION_PROMPTS: dict[str, str] = {
    "food": (
        'Extract food log data. Return JSON only:\n'
        '{"meal_type":"breakfast|lunch|dinner|snack|drink|supplement",'
        '"description":"brief description",'
        '"calories":integer_or_null,"protein_g":null,"carbs_g":null,"fat_g":null,"fiber_g":null,'
        '"foods":[{"name":"...","amount":null,"unit":null,"calories":null}],'
        '"logged_at":"ISO-8601 (use NOW if not visible)",'
        '"notes":null,"confidence":0.0_to_1.0}\n\n'
        'IMPORTANT: Thai, Indian, and composite dishes often hide Avoid/Minimize items — '
        'always decompose into constituent ingredients in the foods[] array. '
        'List each significant ingredient as a separate foods[] entry with its name '
        '(e.g. "Thai green curry" → foods=[{name:"coconut milk",...},{name:"green curry paste",...},'
        '{name:"vegetables",...},{name:"fish sauce",...}]). '
        'For meat dishes, note the cut if visible (e.g. "fatty beef", "lean chicken breast"). '
        'Explicitly surface: coconut (milk/oil/flesh), dairy (ghee/paneer/butter/cream/cheese), '
        'alliums (onion/garlic/leek), high-sugar fruits, fatty cuts.\n\n'
        'NUTRITION LABELS: if a packaged nutrition-facts panel is visible, READ it — copy the '
        'per-serving calories and macros straight into the top-level calories/protein_g/carbs_g/'
        'fat_g/fiber_g fields, scaled by servings consumed (default 1 bottle/pack/can = 1 serving '
        'unless the caption says otherwise). Labels may be in Thai or other languages: map '
        'Energy/พลังงาน→calories(kcal), โปรตีน→protein_g, คาร์โบไฮเดรต→carbs_g, ไขมัน→fat_g, '
        'ใยอาหาร→fiber_g. NEVER return null macros when a label is legible — read the numbers.'
    ),
    "lab": (
        'Extract biomarker measurements from this lab result. Return JSON only:\n'
        '{"biomarkers":[{"extracted_name":"exact name","value":number,"unit":"string",'
        '"measured_at":"ISO-8601 or null","notes":null,"confidence":0.0_to_1.0}]}'
    ),
    "dexa": (
        'Extract DEXA / body composition measurements. Return JSON only:\n'
        '{"biomarkers":[{"extracted_name":"metric name","value":number,"unit":"string",'
        '"measured_at":"ISO-8601 or null","notes":null,"confidence":0.0_to_1.0}]}'
    ),
    "supplement": (
        'Extract supplement intake. Return JSON only:\n'
        '{"name":"supplement name","dose_amount":null,"dose_unit":null,'
        '"taken_at":"ISO-8601 (use NOW if not stated)","notes":null,"confidence":0.0_to_1.0}'
    ),
    "unknown": (
        'You are the router AND extractor for a health logger. Read the message (and any '
        'image) and decide what it IS, then return JSON only — no prose, no markdown.\n\n'
        'DECIDE FIRST: is the user RECORDING something they consumed/measured (a log), or '
        'ASKING for information / making conversation (a brief)?\n'
        'An IMAGE of food, a meal, a drink, a supplement, a label, or a lab/DEXA result is '
        'ALWAYS a log — extract it; never return "brief" when an image is present.\n\n'
        'Brief request — questions, status/summary asks, greetings, thanks, or anything that '
        'is NOT a record of intake/measurement (e.g. "how am I doing?", "brief", "summary", '
        '"what\'s my recovery", "hi", "thanks", "did I take my magnesium?"):\n'
        '{"kind":"brief"}\n\n'
        'Supplement(s) — ALWAYS a list, one object per supplement. A message like '
        '"took D3, K2, B12, magnesium citrate, omega-3" → five objects. Strip filler like '
        '"i took"/"my":\n'
        '{"kind":"supplement","data":[{"name":"...","dose_amount":null,"dose_unit":null,'
        '"taken_at":"NOW","notes":null,"confidence":0.0_to_1.0}]}\n\n'
        'Food or meal — a list, one object per distinct dish:\n'
        '{"kind":"food","data":[{"meal_type":"breakfast|lunch|dinner|snack",'
        '"description":"...","calories":null,"protein_g":null,"carbs_g":null,'
        '"fat_g":null,"fiber_g":null,"logged_at":"NOW","notes":null}]}\n'
        'If a packaged nutrition-facts label is visible, READ it: fill calories/protein_g/'
        'carbs_g/fat_g/fiber_g with the per-serving values (scaled by servings consumed). '
        'Labels may be Thai — Energy/พลังงาน=calories, โปรตีน=protein_g, คาร์โบไฮเดรต=carbs_g, '
        'ไขมัน=fat_g, ใยอาหาร=fiber_g. Do not leave macros null when a label is legible.\n\n'
        'Lab or DEXA result:\n'
        '{"kind":"lab","data":{"biomarkers":[{"extracted_name":"...","value":null,'
        '"unit":"...","measured_at":"NOW","confidence":0.0_to_1.0}]}}\n\n'
        'Genuinely unclassifiable (rare):\n'
        '{"kind":"unknown","data":{}}'
    ),
}


def _strip_fences(text: str) -> str:
    """Remove markdown code fences Claude sometimes adds despite instructions."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:])
        if text.endswith("```"):
            text = text[:-3].strip()
    return text


def _extract_json(text: str):
    """Parse JSON from a model response, tolerating ``` fences and surrounding prose.

    Falls back to the outermost {...} / [...] substring if the whole string isn't valid
    JSON (model added a sentence before/after). Truncated JSON (cut mid-token) still
    raises — that's a max_tokens problem, not a parsing one."""
    t = _strip_fences(text)
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        pass
    for oc, cc in (("{", "}"), ("[", "]")):
        s, e = t.find(oc), t.rfind(cc)
        if 0 <= s < e:
            try:
                return json.loads(t[s:e + 1])
            except json.JSONDecodeError:
                continue
    raise json.JSONDecodeError("no JSON object/array in response", t, 0)


def vision_extract(
    api_key: str,
    model: str,
    image_blocks: list[dict],
    caption: str,
    kind: str,
    now_iso: str,
) -> dict:
    """Call Anthropic Messages API to extract structured data from images + caption.

    Returns the parsed extraction — a dict, or a list of food dicts for multi-item text.
    On a malformed/empty response body returns
    {"confidence": 0.0, "_stage_reason": "vision returned no parseable extraction"} so the
    caller STAGES the item (fail-safe). On HTTP/network failure returns
    {"confidence": 0.0, "_error": "..."} → caller marks it failed.

    Reads HS_ANTHROPIC_API_KEY from the api_key arg (platform reserves ANTHROPIC_API_KEY).
    """
    prompt = _VISION_PROMPTS.get(kind, _VISION_PROMPTS["unknown"]).replace("NOW", now_iso)

    content: list[dict] = [*image_blocks]
    if caption:
        content.append({"type": "text", "text": f"Caption: {caption}"})
    content.append({"type": "text", "text": prompt})

    # 4096 (was 1024): a labelled multi-item food — e.g. "170ml of this shake + a poached
    # egg" with the label's vitamins in foods[] — overflowed 1024 tokens, truncating the
    # JSON mid-string → "no parseable extraction" → stuck in review. Retry once on a parse
    # miss (transient / prose-wrapped); only stage if BOTH attempts fail.
    last_reason = "no parseable extraction"
    for attempt in range(2):
        try:
            resp = httpx.post(
                _ANTHROPIC_URL,
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": _ANTHROPIC_VERSION,
                    "content-type": "application/json",
                },
                json={
                    "model": model,
                    "max_tokens": 4096,
                    "system": (
                        "You are a health data extraction assistant. "
                        "Respond with valid JSON only. No prose, no markdown fences."
                    ),
                    "messages": [{"role": "user", "content": content}],
                },
                timeout=60.0,
            )
            resp.raise_for_status()
            data = resp.json()
            raw = data["content"][0]["text"]
            try:
                return _extract_json(raw)
            except (json.JSONDecodeError, KeyError, IndexError):
                stop = data.get("stop_reason")
                last_reason = f"unparseable JSON (stop_reason={stop})"
                log.warning("vision_extract parse miss (attempt %d, stop_reason=%s): %.200s",
                            attempt + 1, stop, raw)
                continue  # retry once
        except (KeyError, IndexError) as exc:
            last_reason = f"unexpected response shape: {exc}"
            log.warning("vision_extract bad shape (attempt %d): %s", attempt + 1, exc)
            continue
        except Exception as exc:
            return {"confidence": 0.0, "_error": str(exc)}
    return {"confidence": 0.0, "_stage_reason": f"vision returned {last_reason}"}


def content_cluster_ungrouped(
    api_key: str,
    model: str,
    items: list[dict],
) -> list[list[dict]]:
    """Group ungrouped items by content similarity using a text-only API call.

    Returns list of clusters (each a list of items). Single item → [[item]].
    Items not mentioned by the API become singletons.
    """
    if len(items) <= 1:
        return [[item] for item in items]

    item_descs = "\n".join(
        f"{i+1}. id={item['id']} kind={item.get('kind','?')} "
        f"caption={str(item.get('caption') or '')[:100]!r}"
        for i, item in enumerate(items)
    )
    id_list = [item["id"] for item in items]

    try:
        resp = httpx.post(
            _ANTHROPIC_URL,
            headers={
                "x-api-key": api_key,
                "anthropic-version": _ANTHROPIC_VERSION,
                "content-type": "application/json",
            },
            json={
                "model": model,
                "max_tokens": 512,
                "system": "You group health data items. Respond with valid JSON only.",
                "messages": [{
                    "role": "user",
                    "content": (
                        f"Group these {len(items)} health items. "
                        "Items showing the SAME product, meal, or measurement go together. "
                        "When ambiguous, keep separate. "
                        f'Return JSON only: {{"groups": [["id1","id2"],["id3"],...]}}\n\n'
                        f"Items:\n{item_descs}\n\nAvailable IDs: {id_list}"
                    ),
                }],
            },
            timeout=30.0,
        )
        resp.raise_for_status()
        parsed = json.loads(_strip_fences(resp.json()["content"][0]["text"]))
        id_to_item = {item["id"]: item for item in items}
        seen: set[str] = set()
        clusters: list[list[dict]] = []
        for group in parsed.get("groups", []):
            cluster = [id_to_item[g] for g in group if g in id_to_item and g not in seen]
            if cluster:
                clusters.append(cluster)
                seen.update(r["id"] for r in cluster)
        for item in items:
            if item["id"] not in seen:
                clusters.append([item])
        return clusters
    except Exception:
        return [[item] for item in items]


# ── telegram ──────────────────────────────────────────────────────────────────

def telegram_send(token: str, chat_id: int, text: str) -> None:
    """Best-effort Telegram sendMessage. Never raises."""
    try:
        httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=10.0,
        )
    except Exception:
        pass


def compose_confirmation(kind: str, rpc_status: str, extracted, is_minor: bool) -> str:
    """Compose Telegram confirmation. Minor-safe: no deficit/restriction language.

    `extracted` may be a dict (single item) or a list (multi-item food/supplement).
    """
    items = extracted if isinstance(extracted, list) else [extracted]
    dicts = [x for x in items if isinstance(x, dict)]
    first = dicts[0] if dicts else {}
    staged_msg = "📋 I'll check this one with PC. Thanks!" if is_minor else "📋 Queued for review."

    if kind == "food" and rpc_status in ("inserted", "staged"):
        if rpc_status == "staged":
            return staged_msg
        if len(dicts) > 1:
            descs = ", ".join((d.get("description") or "item") for d in dicts)
            return (f"📊 Tracked {len(dicts)} items: {descs}. Nice! 💪" if is_minor
                    else f"✅ Logged {len(dicts)} items: {descs}.")
        desc = first.get("description") or "meal"
        kcal = first.get("calories")
        kcal_str = f" — {kcal} kcal" if kcal else ""
        return (
            f"📊 Tracked: {desc}{kcal_str}. Nice! 💪" if is_minor
            else f"✅ Logged: {desc}{kcal_str}."
        )
    if kind in ("lab", "dexa"):
        n = len(first.get("biomarkers", [])) or 1
        if is_minor:
            return f"📊 {n} measurement{'s' if n > 1 else ''} tracked. 💪"
        return f"✅ {n} biomarker{'s' if n > 1 else ''} logged."
    if kind == "supplement":
        if rpc_status == "staged":
            return staged_msg
        names = [d.get("name") for d in dicts if d.get("name")]
        if len(names) > 1:
            joined = ", ".join(names)
            return (f"📊 {len(names)} supplements tracked: {joined}." if is_minor
                    else f"✅ Logged {len(names)} supplements: {joined}.")
        name = names[0] if names else "supplement"
        return f"📊 {name} tracked." if is_minor else f"✅ {name} logged."
    if rpc_status == "staged":
        return staged_msg
    return "✅ Logged."


# ── running totals (PostgREST — no psycopg2 in cloud) ────────────────────────

def _load_profile_contexts(identities: list[dict]) -> dict[str, dict]:
    """Map profile_id → parsed context dict from context/<slug>.context.md.
    Missing or unreadable context files → empty dict for that profile.
    """
    try:
        from lib.context import load_context
    except ImportError:
        return {}
    result: dict[str, dict] = {}
    for ident in identities:
        pid = str(ident.get("profile_id") or "")
        name = str(ident.get("display_name") or "")
        if not pid or not name:
            continue
        slug = name.strip().lower()
        try:
            result[pid] = load_context(slug)
        except Exception:
            result[pid] = {}
    return result


def fetch_today_food_totals(db: DbRest, profile_id: str, today: str) -> dict:
    """Today's food_logs macros via PostgREST. Returns {} on any error (best-effort)."""
    try:
        rows = db.select(
            "food_logs",
            select="calories,protein_g,carbs_g,fat_g",
            filters={
                "profile_id": f"eq.{profile_id}",
                "log_date": f"eq.{today}",
                "is_day_summary": "not.is.true",
            },
        )
        kcal = round(sum(float(r.get("calories") or 0) for r in rows))
        protein = round(sum(float(r.get("protein_g") or 0) for r in rows), 1)
        return {"kcal": kcal, "protein_g": protein, "meals": len(rows)}
    except Exception:
        return {}


def fetch_today_supplement_counts(db: DbRest, profile_id: str, today: str) -> dict:
    """Active regimen count vs today's intake_logs. Returns {} on any error or no regimen."""
    try:
        regimens = db.select(
            "supplement_regimens",
            select="supplement_id",
            filters={
                "profile_id": f"eq.{profile_id}",
                "status": "eq.active",
                "start_date": f"lte.{today}",
                "or": f"(end_date.is.null,end_date.gte.{today})",
            },
        )
        total = len(regimens)
        if not total:
            return {}
        regimen_ids = {r["supplement_id"] for r in regimens}
        intakes = db.select(
            "supplement_intake_logs",
            select="supplement_id",
            filters={"profile_id": f"eq.{profile_id}", "taken_on": f"eq.{today}"},
        )
        taken = sum(1 for r in intakes if r.get("supplement_id") in regimen_ids)
        return {"taken": taken, "total": total}
    except Exception:
        return {}


def _totals_line(
    totals: dict,
    supp: dict,
    target_cal,
    target_protein_g,
    is_minor: bool,
) -> str:
    """One-line running-total appended to the Telegram confirmation after a food write.

    Minor-safe: no deficit or restriction language.
    Returns '' when totals is empty (query failed or no rows yet).
    """
    if not totals:
        return ""
    kcal = totals.get("kcal", 0)
    protein = totals.get("protein_g", 0)

    if is_minor:
        line = f"Today: {kcal} kcal · {protein}g protein"
        # Flag low intake with growth/performance framing only — never "not enough"
        if target_cal and kcal < 0.6 * float(target_cal):
            line += " — keep fuelling to stay strong 💪"
        return line

    # Adult
    if target_cal:
        pct = round(100.0 * kcal / float(target_cal)) if target_cal else 0
        cal_str = f"{kcal} / {int(target_cal)} kcal ({pct}%)"
    else:
        cal_str = f"{kcal} kcal"

    if target_protein_g:
        prot_str = f"{protein} / {int(target_protein_g)}g protein"
    else:
        prot_str = f"{protein}g protein"

    line = f"Today: {cal_str} · {prot_str}"
    if supp and supp.get("total"):
        line += f" · Supps {supp['taken']}/{supp['total']}"
    return line


# ── main loop ─────────────────────────────────────────────────────────────────

def _process_cluster(
    db: DbRest,
    cluster: list[dict],
    api_key: str,
    model: str,
    now_iso: str,
    conf_threshold: float,
    chat_is_minor: dict[str, bool],
    token: str,
    summary: dict,
    *,
    profile_ctx: dict | None = None,
    per_chat: dict | None = None,
    today: str | None = None,
    learn_min_logs: int = 2,
    brief_profiles: set | None = None,
) -> None:
    """Extract, write, and confirm one cluster. Updates summary in place.

    A message the LLM classifies as a brief request adds its profile_id to
    brief_profiles (sent once at end-of-run by run_once) and writes nothing.
    """
    profile_id = cluster[0]["profile_id"]
    chat_id = cluster[0]["chat_id"]
    is_minor = chat_is_minor.get(str(chat_id), False)
    kind = cluster[0].get("kind") or "unknown"
    caption = merge_caption(cluster)
    raw_text = caption

    # Download images
    image_blocks: list[dict] = []
    for row in cluster:
        path = row.get("storage_path")
        if path:
            url = get_signed_url(db, path)
            if url:
                img = _download_image(url)
                if img:
                    image_blocks.append(_image_block(*img))

    # Vision extraction
    extracted = vision_extract(api_key, model, image_blocks, caption, kind, now_iso)

    # Guard: vision_extract may return a list (multi-item food) or a dict with
    # _error/_stage_reason. Normalize before checking sentinel keys.
    if not isinstance(extracted, dict):
        # Treat non-dict as a food list — handled in the food branch below
        pass
    elif "_stage_reason" in extracted:
        # Recoverable parse failure — stage for review, not a hard fail
        mark_rows(db, _ids(cluster), "staged", stage_reason=extracted["_stage_reason"])
        summary["staged"] += 1
        if per_chat is not None:
            per_chat.setdefault(str(chat_id), {"written": 0, "staged": 0})["staged"] += 1
        msg = "📋 I'll check this one with PC. Thanks!" if is_minor else "📋 Queued for review."
        telegram_send(token, chat_id, msg)
        return
    elif "_error" in extracted:
        summary["failed"] += 1
        summary["errors"].append(extracted["_error"])
        mark_rows(db, _ids(cluster), "failed", stage_reason=extracted["_error"])
        return

    # Handle "unknown" kind — re-dispatch based on what Claude returned
    if isinstance(extracted, dict) and kind == "unknown" and "kind" in extracted:
        kind = extracted.get("kind", "unknown")
        extracted = extracted.get("data", extracted)

    # food and supplement branches handle a bare list (multi-item); for other kinds
    # coerce to the first dict (or {}) so a shape surprise stages rather than crashing.
    if isinstance(extracted, list) and kind not in ("food", "supplement"):
        extracted = extracted[0] if extracted and isinstance(extracted[0], dict) else {}

    confidence = float(extracted.get("confidence", 0.0)) if isinstance(extracted, dict) else 0.0
    rpc_status = "failed"
    n_written = 1  # how many rows this cluster wrote — multi-item food/supplement set this

    viome_verdicts: list[dict] = []
    learn_offer: str = ""
    meal_verdict: str = "clean"
    meal_flags: list[str] = []

    # Brief request — the LLM decided this message is a question/summary/greeting, not a
    # log. Record the profile so run_once sends ONE brief at end-of-run; write nothing.
    if kind == "brief":
        if brief_profiles is not None:
            brief_profiles.add(str(profile_id))
        mark_rows(db, _ids(cluster), "done")
        return

    if kind == "food":
        # Normalise: model may return a list for multi-item text (e.g. "shake + fish + rice")
        food_items: list[dict] = extracted if isinstance(extracted, list) else [extracted]
        statuses: list[str] = []
        inserted_ids: list[str] = []   # food_logs.id for rows we successfully inserted
        food_stage_reason: str | None = None  # first stage_reason from any staged RPC result
        has_ref_miss = False  # any item without a food_reference hit
        for food_item in food_items:
            if not isinstance(food_item, dict):
                statuses.append("failed")
                continue

            # Food reference resolution: caption → description → ingredient names
            candidates = _food_reference_candidates(raw_text, food_item)
            ref = lookup_food_reference(db, candidates, str(profile_id))
            if ref:
                food_item["calories"]   = ref["calories"]
                food_item["protein_g"]  = ref["protein_g"]
                food_item["carbs_g"]    = ref["carbs_g"]
                food_item["fat_g"]      = ref["fat_g"]
                if ref.get("fiber_g") is not None:
                    food_item["fiber_g"] = ref["fiber_g"]
            else:
                has_ref_miss = True

            item_conf = float(food_item.get("confidence", confidence))
            is_complete = food_is_complete(food_item)
            item_stage_reason = None if is_complete else "incomplete: missing calories or macros"
            try:
                res = db.rpc("maintainer_ingest_food", _food_rpc_args(
                    profile_id, food_item, item_conf, raw_text,
                    not is_complete,
                    item_stage_reason,
                )) or {}
                s = res.get("status", "failed")
                statuses.append(s)
                if s == "inserted" and res.get("id"):
                    inserted_ids.append(str(res["id"]))
                if s == "staged" and food_stage_reason is None:
                    food_stage_reason = res.get("stage_reason") or item_stage_reason
            except Exception as exc:
                log.error("maintainer_ingest_food failed: %s", exc)
                statuses.append("failed")

        # Aggregate: all inserted → done; any staged (none failed) → staged; else failed
        if not statuses or "failed" in statuses:
            rpc_status = "failed"
            n_ok = sum(1 for s in statuses if s in ("inserted", "staged"))
            n_total = len(food_items)
            partial_reason = (
                f"partial: {n_ok} of {n_total} items wrote successfully"
                if n_ok > 0
                else None
            )
            mark_rows(db, _ids(cluster), "failed", stage_reason=partial_reason)
        elif "staged" in statuses:
            rpc_status = "staged"
            mark_rows(db, _ids(cluster), "staged", stage_reason=food_stage_reason)
        else:
            rpc_status = "inserted"
            mark_rows(db, _ids(cluster), "done")

        # Viome cross-check — collect all ingredient + description candidates across items
        if rpc_status == "inserted" and not is_minor:
            viome_candidates: list[str] = []
            seen_vc: set[str] = set()
            for fi in food_items:
                if not isinstance(fi, dict):
                    continue
                for c in ([f.get("name") or "" for f in (fi.get("foods") or [])]
                           + [fi.get("description") or ""]):
                    cl = c.strip().lower()
                    if cl and cl not in seen_vc:
                        seen_vc.add(cl)
                        viome_candidates.append(c.strip())
            if viome_candidates:
                viome_verdicts = lookup_viome_verdicts(db, viome_candidates, str(profile_id))

            # Compute meal verdict and store on food_logs (best-effort)
            meal_verdict, meal_flags = compute_meal_verdict(viome_verdicts)
            if inserted_ids:
                try:
                    db.update(
                        "food_logs",
                        {"id": f"in.({','.join(inserted_ids)})"},
                        {"verdict": meal_verdict, "flags": meal_flags},
                    )
                except Exception as exc:
                    log.warning("verdict UPDATE on food_logs failed: %s", exc)

        # Learn-from-past offer — only when we had no reference hit and food was written
        if rpc_status == "inserted" and has_ref_miss and not is_minor:
            for fi in food_items:
                if not isinstance(fi, dict):
                    continue
                desc = fi.get("description") or ""
                if desc:
                    past = lookup_past_food_macros(db, desc, str(profile_id), learn_min_logs)
                    if past:
                        learn_offer = (
                            f"💡 Logged '{desc}' {past['count']}× before — "
                            "use /learn to add it to your food library."
                        )
                        break

        # Keep the FULL item list so the confirmation names every item, not just the first
        # (a 2-item "pineapple and dragon fruit" must not report as just "Pineapple").
        extracted = [fi for fi in food_items if isinstance(fi, dict)] or [{}]
        n_written = sum(1 for s in statuses if s == "inserted")

    elif kind in ("lab", "dexa"):
        biomarkers = extracted.get("biomarkers", [])
        all_ok = True
        for bio in biomarkers:
            matches = lookup_metric(db, bio.get("extracted_name", ""))
            if matches:
                bio["metric_definition_id"] = matches[0]["id"]
            complete = biomarker_is_complete(bio)
            if not complete:
                reason = (
                    "incomplete: no metric match" if not bio.get("metric_definition_id")
                    else "incomplete: missing value or unit"
                )
                result = write_biomarker(
                    db, cluster, profile_id, bio,
                    float(bio.get("confidence", 0.0)), raw_text,
                    force_stage=True, stage_reason=reason,
                )
            else:
                result = write_biomarker(
                    db, cluster, profile_id, bio,
                    float(bio.get("confidence", 0.0)), raw_text,
                )
            if result.get("status") not in ("inserted", "staged"):
                all_ok = False
        rpc_status = "inserted" if all_ok and biomarkers else "staged"

    elif kind == "supplement":
        # One or many supplements ("took D3, K2, magnesium" → 3). Mirror the food
        # multi-item loop: resolve + RPC each, then mark the cluster rows once by
        # aggregate status so we never double-mark.
        supp_items: list[dict] = extracted if isinstance(extracted, list) else [extracted]
        statuses: list[str] = []
        supp_stage_reason: str | None = None
        for supp in supp_items:
            if not isinstance(supp, dict):
                statuses.append("failed")
                continue
            name = supp.get("name", "")
            matches = lookup_supplement_by_name(db, name) if name else []
            if matches:
                supp["supplement_id"] = matches[0]["id"]
            # Default the dose from the user's regimen when not stated ("took my magnesium
            # citrate" → the prescribed 200 mg) so it auto-logs instead of staging.
            if supp.get("supplement_id") and (
                supp.get("dose_amount") is None or not supp.get("dose_unit")
            ):
                reg = lookup_regimen_dose(db, str(profile_id), supp["supplement_id"])
                if reg:
                    if supp.get("dose_amount") is None:
                        supp["dose_amount"] = reg["dose_amount"]
                    if not supp.get("dose_unit"):
                        supp["dose_unit"] = reg["dose_unit"]
                    supp["notes"] = ((supp.get("notes") or "") + " [dose from regimen]").strip()
            complete = supplement_is_complete(supp)
            # Only an unresolved name stages now — dose is optional (logged null if absent).
            item_reason = None if complete else "incomplete: no supplement match"
            try:
                res = db.rpc("maintainer_ingest_supplement", _supplement_rpc_args(
                    profile_id, supp, float(supp.get("confidence", confidence)),
                    raw_text, not complete, item_reason,
                )) or {}
                s = res.get("status", "failed")
                statuses.append(s)
                if s == "staged" and supp_stage_reason is None:
                    supp_stage_reason = res.get("stage_reason") or item_reason
            except Exception as exc:
                log.error("maintainer_ingest_supplement failed: %s", exc)
                statuses.append("failed")

        # Aggregate: all inserted → done; any staged (none failed) → staged; else failed
        if not statuses or "failed" in statuses:
            rpc_status = "failed"
            n_ok = sum(1 for s in statuses if s in ("inserted", "staged"))
            partial = (f"partial: {n_ok} of {len(supp_items)} supplements wrote successfully"
                       if n_ok else None)
            mark_rows(db, _ids(cluster), "failed", stage_reason=partial)
        elif "staged" in statuses:
            rpc_status = "staged"
            mark_rows(db, _ids(cluster), "staged", stage_reason=supp_stage_reason)
        else:
            rpc_status = "inserted"
            mark_rows(db, _ids(cluster), "done")
        n_written = sum(1 for s in statuses if s == "inserted")
        # `extracted` stays the list — compose_confirmation handles it.

    else:
        # workout / could not classify — always stage as food for human review.
        # force_stage=True required: maintainer_ingest_food routes on p_force_stage, not confidence.
        if kind == "unknown":
            stage_rsn = "could not classify"
        else:
            stage_rsn = f"unknown kind: {kind}"
        extracted.setdefault("meal_type", "unknown")
        extracted.setdefault("description", caption or "unclassified item")
        result = write_food(db, cluster, profile_id, extracted, 0.0, raw_text,
                            force_stage=True, stage_reason=stage_rsn)
        rpc_status = result.get("status", "failed")

    if rpc_status == "inserted":
        summary["written"] += n_written
        if per_chat is not None:
            per_chat.setdefault(str(chat_id), {"written": 0, "staged": 0})["written"] += n_written
    elif rpc_status == "staged":
        summary["staged"] += 1
        if per_chat is not None:
            per_chat.setdefault(str(chat_id), {"written": 0, "staged": 0})["staged"] += 1
    else:
        summary["failed"] += 1
        summary["errors"].append(f"cluster {_ids(cluster)} rpc returned {rpc_status!r}")

    # Telegram confirmation — append running food totals after a successful write
    msg = compose_confirmation(kind, rpc_status, extracted, is_minor)

    # Viome cross-check lines (food, inserted, adult only)
    if kind == "food" and rpc_status == "inserted" and not is_minor and viome_verdicts:
        vlines = _viome_verdict_lines(viome_verdicts)
        if vlines:
            msg = f"{msg}\n{vlines}"

    # Learn-from-past offer (food, inserted, no reference hit, adult only)
    if kind == "food" and rpc_status == "inserted" and not is_minor and learn_offer:
        msg = f"{msg}\n{learn_offer}"

    if rpc_status == "inserted" and kind == "food" and today and profile_ctx is not None:
        ctx = profile_ctx.get(str(profile_id), {})
        target_cal = (ctx.get("targets") or {}).get("daily_calories")
        target_prot = (ctx.get("targets") or {}).get("protein_g")
        totals = fetch_today_food_totals(db, str(profile_id), today)
        supp = {} if is_minor else fetch_today_supplement_counts(db, str(profile_id), today)
        tline = _totals_line(totals, supp, target_cal, target_prot, is_minor)
        if tline:
            msg = f"{msg}\n{tline}"
    telegram_send(token, chat_id, msg)


def run_once(db: DbRest, cfg: dict, api_key: str, token: str, settle_sec_override: int | None = None) -> dict:
    """Full drain loop. Returns JSON-serialisable summary dict."""
    summary: dict[str, Any] = {
        "fetched": 0, "clustered": 0,
        "written": 0, "staged": 0, "failed": 0,
        "errors": [],
    }

    settle_sec = settle_sec_override if settle_sec_override is not None else int(cfg.get("push.inbox_settle_sec", _SETTLE_SEC_FALLBACK))
    conf_threshold = float(cfg.get("ingest.confidence_threshold", _CONFIDENCE_FALLBACK))
    model = str(cfg.get("drain.vision_model", _VISION_MODEL_FALLBACK)).strip('"')
    learn_min_logs = int(cfg.get("food_reference.learn_min_logs", 2))
    now_iso = datetime.now(timezone.utc).isoformat()
    today_date = datetime.now(timezone.utc).date().isoformat()

    # Identity maps for Telegram confirmations and context MD lookup
    identities = db.select(
        "telegram_identities",
        select="chat_id,is_minor,profile_id,display_name",
        filters={"status": "eq.active"},
    )
    chat_is_minor = {str(r["chat_id"]): bool(r.get("is_minor")) for r in identities}
    profile_ctx = _load_profile_contexts(identities)
    per_chat: dict[str, dict] = {}
    brief_profiles: set[str] = set()  # profiles whose text the LLM classified as a brief request

    items = fetch_settled(db, settle_sec)
    summary["fetched"] = len(items)
    if not items:
        return summary

    # Album clusters (deterministic) + content clusters (per-chat API call)
    album_clusters, ungrouped = build_clusters(items)

    by_chat: dict[str, list[dict]] = defaultdict(list)
    for item in ungrouped:
        by_chat[str(item["chat_id"])].append(item)

    content_clusters: list[list[dict]] = []
    for chat_items in by_chat.values():
        content_clusters.extend(content_cluster_ungrouped(api_key, model, chat_items))

    all_clusters = album_clusters + content_clusters
    summary["clustered"] = len(all_clusters)

    for cluster in all_clusters:
        # Atomic claim — all rows or none
        claimed: list[str] = []
        skipped = False
        for row in cluster:
            if claim(db, row["id"]):
                claimed.append(row["id"])
            else:
                skipped = True
                break

        if skipped:
            # Release any rows we claimed back to pending
            if claimed:
                db.update(
                    "media_inbox",
                    {"id": f"in.({','.join(claimed)})", "status": "eq.processing"},
                    {"status": "pending"},
                )
            continue

        try:
            _process_cluster(
                db, cluster, api_key, model, now_iso,
                conf_threshold, chat_is_minor, token, summary,
                profile_ctx=profile_ctx, per_chat=per_chat, today=today_date,
                learn_min_logs=learn_min_logs, brief_profiles=brief_profiles,
            )
        except Exception as exc:
            summary["failed"] += 1
            summary["errors"].append(str(exc))
            mark_rows(db, _ids(cluster), "failed")

    # End-of-run summary — one message per chat that had activity
    for chat_id_str, stats in per_chat.items():
        w, s = stats["written"], stats["staged"]
        if w + s == 0:
            continue
        parts: list[str] = []
        if w:
            parts.append(f"{w} logged")
        if s:
            parts.append(f"{s} to review")
        telegram_send(token, int(chat_id_str), f"Done — {', '.join(parts)}.")

    # Compose and send daily brief — best-effort. Two triggers:
    #   1. an adult profile that had a food write (post-log nudge), and
    #   2. any profile whose message the LLM classified as a brief request.
    # Import here to avoid circular imports at module level.
    written_adult_profiles: set[str] = {
        str(r["profile_id"])
        for r in identities
        if not r.get("is_minor")
        and per_chat.get(str(r["chat_id"]), {}).get("written", 0) > 0
    }
    brief_targets = written_adult_profiles | brief_profiles
    if brief_targets:
        try:
            from monitor.brief import compose_brief
            for pid in brief_targets:
                try:
                    compose_brief(db, pid, cfg, api_key, token, today_date)
                except Exception as exc:
                    log.warning("compose_brief failed for %s: %s", pid, exc)
        except ImportError:
            pass  # brief module not yet available — silently skip

    return summary


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    import os
    import sys

    from lib.db_rest import sign_in

    parser = argparse.ArgumentParser(description="HealthSpan inbox drain")
    parser.add_argument("--once", action="store_true", help="Run full drain loop and exit")
    parser.add_argument("--settle-sec", type=int, default=None, metavar="SEC",
                        help="Override inbox settle window (default: push.inbox_settle_sec config)")
    args = parser.parse_args()

    url = os.environ["SUPABASE_URL"]
    anon = os.environ["SUPABASE_ANON_KEY"]
    email = os.environ["HS_AUTH_EMAIL"]
    pwd = os.environ["HS_AUTH_PASSWORD"]
    api_key = os.environ["HS_ANTHROPIC_API_KEY"]
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")

    jwt = sign_in(url, anon, email, pwd)

    with DbRest(url, anon, jwt) as db:
        cfg = get_config(db)

        if args.once:
            summary = run_once(db, cfg, api_key, token, settle_sec_override=args.settle_sec)
            print(json.dumps(summary))
            sys.exit(0 if summary["failed"] == 0 else 1)

        # Default: dry-run summary (no writes)
        settle = int(cfg.get("push.inbox_settle_sec", _SETTLE_SEC_FALLBACK))
        items = fetch_settled(db, settle)
        album_clusters, ungrouped = build_clusters(items)
        print(f"Settled: {len(items)} items | {len(album_clusters)} album clusters | {len(ungrouped)} ungrouped")
        for cluster in album_clusters:
            print(f"  Album {cluster[0].get('media_group_id')}: {len(cluster)} rows, caption={merge_caption(cluster)!r}")
        for item in ungrouped:
            print(f"  Ungrouped id={item['id']} kind={item.get('kind')} caption={str(item.get('caption',''))[:60]!r}")
