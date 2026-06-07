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


def write_biomarker(
    db: DbRest, rows: list[dict], profile_id: str,
    extracted: dict[str, Any], confidence: float, raw_text: str,
) -> dict:
    return _write(db, rows, "maintainer_ingest_biomarker", {
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
    })


def write_supplement(
    db: DbRest, rows: list[dict], profile_id: str,
    extracted: dict[str, Any], confidence: float, raw_text: str,
) -> dict:
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


# ── lookups ───────────────────────────────────────────────────────────────────

def lookup_metric(db: DbRest, query: str) -> list[dict]:
    return db.select(
        "metric_definitions",
        select="id,name,display_name,unit",
        filters={"name": f"ilike.*{query}*", "is_active": "eq.true"},
        limit=5,
    )


def lookup_supplement_by_name(db: DbRest, query: str) -> list[dict]:
    return db.select(
        "supplements",
        select="id,name,display_name,default_unit",
        filters={"name": f"ilike.*{query}*", "is_active": "eq.true"},
        limit=5,
    )


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
        '{"meal_type":"breakfast|lunch|dinner|snack|supplement|unknown",'
        '"description":"brief description",'
        '"calories":integer_or_null,"protein_g":null,"carbs_g":null,"fat_g":null,"fiber_g":null,'
        '"foods":[{"name":"...","amount":null,"unit":null,"calories":null}],'
        '"logged_at":"ISO-8601 (use NOW if not visible)",'
        '"notes":null,"confidence":0.0_to_1.0}'
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
        'Classify and extract health data. Return JSON only:\n'
        '{"kind":"food|lab|supplement|workout|unknown","data":{...kind-specific fields...},'
        '"confidence":0.0_to_1.0}'
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
                "max_tokens": 1024,
                "system": (
                    "You are a health data extraction assistant. "
                    "Respond with valid JSON only. No prose, no markdown fences."
                ),
                "messages": [{"role": "user", "content": content}],
            },
            timeout=60.0,
        )
        resp.raise_for_status()
        raw = resp.json()["content"][0]["text"]
        return json.loads(_strip_fences(raw))
    except (json.JSONDecodeError, KeyError, IndexError):
        # Malformed or empty API response body — stage for review, not a hard failure
        return {"confidence": 0.0, "_stage_reason": "vision returned no parseable extraction"}
    except Exception as exc:
        return {"confidence": 0.0, "_error": str(exc)}


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


def compose_confirmation(kind: str, rpc_status: str, extracted: dict, is_minor: bool) -> str:
    """Compose Telegram confirmation. Minor-safe: no deficit/restriction language."""
    if kind == "food" and rpc_status in ("inserted", "staged"):
        desc = extracted.get("description") or "meal"
        kcal = extracted.get("calories")
        kcal_str = f" — {kcal} kcal" if kcal else ""
        if rpc_status == "staged":
            return "📋 I'll check this one with PC. Thanks!" if is_minor else "📋 Queued for review."
        return (
            f"📊 Tracked: {desc}{kcal_str}. Nice! 💪" if is_minor
            else f"✅ Logged: {desc}{kcal_str}."
        )
    if kind in ("lab", "dexa"):
        n = len(extracted.get("biomarkers", [])) or 1
        if is_minor:
            return f"📊 {n} measurement{'s' if n > 1 else ''} tracked. 💪"
        return f"✅ {n} biomarker{'s' if n > 1 else ''} logged."
    if kind == "supplement":
        name = extracted.get("name") or "supplement"
        if is_minor:
            return f"📊 {name} tracked."
        return f"✅ {name} logged."
    if rpc_status == "staged":
        return "📋 I'll check this one with PC. Thanks!" if is_minor else "📋 Queued for review."
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
) -> None:
    """Extract, write, and confirm one cluster. Updates summary in place."""
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

    # Only the food branch handles a bare list; for other kinds coerce to the first
    # dict (or {}) so a shape surprise stages rather than crashing the cluster.
    if isinstance(extracted, list) and kind != "food":
        extracted = extracted[0] if extracted and isinstance(extracted[0], dict) else {}

    confidence = float(extracted.get("confidence", 0.0)) if isinstance(extracted, dict) else 0.0
    rpc_status = "failed"

    if kind == "food":
        # Normalise: model may return a list for multi-item text (e.g. "shake + fish + rice")
        food_items: list[dict] = extracted if isinstance(extracted, list) else [extracted]
        statuses: list[str] = []
        food_stage_reason: str | None = None  # first stage_reason from any staged RPC result
        for food_item in food_items:
            if not isinstance(food_item, dict):
                statuses.append("failed")
                continue
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
                if s == "staged" and food_stage_reason is None:
                    food_stage_reason = res.get("stage_reason") or item_stage_reason
            except Exception as exc:
                log.error("maintainer_ingest_food failed: %s", exc)
                statuses.append("failed")
        # Aggregate: all inserted → done; any staged (none failed) → staged; else failed
        if not statuses or "failed" in statuses:
            rpc_status = "failed"
            mark_rows(db, _ids(cluster), "failed")
        elif "staged" in statuses:
            rpc_status = "staged"
            mark_rows(db, _ids(cluster), "staged", stage_reason=food_stage_reason)
        else:
            rpc_status = "inserted"
            mark_rows(db, _ids(cluster), "done")
        # Use first item for confirmation message
        extracted = food_items[0] if food_items and isinstance(food_items[0], dict) else {}

    elif kind in ("lab", "dexa"):
        biomarkers = extracted.get("biomarkers", [])
        all_ok = True
        for bio in biomarkers:
            # Resolve metric_definition_id by extracted_name
            matches = lookup_metric(db, bio.get("extracted_name", ""))
            if matches:
                bio["metric_definition_id"] = matches[0]["id"]
            else:
                bio["confidence"] = 0.0  # force staging — maintainer resolves
            result = write_biomarker(db, cluster, profile_id, bio, float(bio.get("confidence", 0.0)), raw_text)
            if result.get("status") not in ("inserted", "staged"):
                all_ok = False
        rpc_status = "inserted" if all_ok and biomarkers else "staged"

    elif kind == "supplement":
        name = extracted.get("name", "")
        matches = lookup_supplement_by_name(db, name) if name else []
        if matches:
            extracted["supplement_id"] = matches[0]["id"]
        else:
            extracted["confidence"] = 0.0  # force staging — maintainer resolves
            confidence = 0.0
        result = write_supplement(db, cluster, profile_id, extracted, confidence, raw_text)
        rpc_status = result.get("status", "failed")

    else:
        # workout / truly unknown — always stage as food for human review.
        # force_stage=True is required: maintainer_ingest_food now routes on p_force_stage,
        # not confidence, so without it a 0.0-confidence unknown item would auto-write to prod.
        extracted.setdefault("meal_type", "unknown")
        extracted.setdefault("description", caption or "unknown item")
        result = write_food(db, cluster, profile_id, extracted, 0.0, raw_text,
                            force_stage=True, stage_reason=f"unknown kind: {kind}")
        rpc_status = result.get("status", "failed")

    if rpc_status == "inserted":
        summary["written"] += 1
        if per_chat is not None:
            per_chat.setdefault(str(chat_id), {"written": 0, "staged": 0})["written"] += 1
    elif rpc_status == "staged":
        summary["staged"] += 1
        if per_chat is not None:
            per_chat.setdefault(str(chat_id), {"written": 0, "staged": 0})["staged"] += 1
    else:
        summary["failed"] += 1
        summary["errors"].append(f"cluster {_ids(cluster)} rpc returned {rpc_status!r}")

    # Telegram confirmation — append running food totals after a successful write
    msg = compose_confirmation(kind, rpc_status, extracted, is_minor)
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


def run_once(db: DbRest, cfg: dict, api_key: str, token: str) -> dict:
    """Full drain loop. Returns JSON-serialisable summary dict."""
    summary: dict[str, Any] = {
        "fetched": 0, "clustered": 0,
        "written": 0, "staged": 0, "failed": 0,
        "errors": [],
    }

    settle_sec = int(cfg.get("push.inbox_settle_sec", _SETTLE_SEC_FALLBACK))
    conf_threshold = float(cfg.get("ingest.confidence_threshold", _CONFIDENCE_FALLBACK))
    model = str(cfg.get("drain.vision_model", _VISION_MODEL_FALLBACK)).strip('"')
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

    return summary


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    import os
    import sys

    from lib.db_rest import sign_in

    parser = argparse.ArgumentParser(description="HealthSpan inbox drain")
    parser.add_argument("--once", action="store_true", help="Run full drain loop and exit")
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
            summary = run_once(db, cfg, api_key, token)
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
