"""Unit tests for monitor/inbox_drain.py — Phase 3B autonomous drain.

All tests mocked — no live DB, no Anthropic API, no Telegram.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from lib.db_rest import DbRest
from monitor.inbox_drain import (
    _image_block,
    _strip_fences,
    build_clusters,
    compose_confirmation,
    content_cluster_ungrouped,
    merge_caption,
    run_once,
    vision_extract,
)


# ── helpers ───────────────────────────────────────────────────────────────────

def _db(responses: list[tuple[int, Any]]) -> DbRest:
    idx = 0

    def _handler(request: httpx.Request) -> httpx.Response:
        nonlocal idx
        status, body = responses[idx % len(responses)]
        idx += 1
        # Build response with explicit content bytes so .json() works reliably
        content = json.dumps(body).encode()
        return httpx.Response(status, content=content,
                              headers={"content-type": "application/json"})

    client = httpx.Client(transport=httpx.MockTransport(_handler))
    db = DbRest.__new__(DbRest)
    db._base = "https://test.supabase.co"
    db._anon_key = "anon"
    db._jwt = "jwt"
    db._client = client
    return db


def _mock_http_resp(body: Any) -> MagicMock:
    """Mock httpx response: .json() returns body, .raise_for_status() is a no-op."""
    r = MagicMock()
    r.json.return_value = body
    r.raise_for_status = MagicMock()
    return r


def _item(id: str, kind: str = "food", mgid: str | None = None, caption: str = "") -> dict:
    return {
        "id": id,
        "profile_id": "21f69003-46f8-4e1c-a928-b1f694ce4aff",
        "chat_id": 99,
        "kind": kind,
        "storage_path": None,
        "caption": caption,
        "media_group_id": mgid,
        "status": "pending",
        "created_at": "2026-06-07T10:00:00Z",
    }


_FOOD_EXTRACTION = {
    "meal_type": "lunch",
    "description": "grilled chicken",
    "calories": 450,
    "protein_g": 40,
    "carbs_g": 20,
    "fat_g": 12,
    "fiber_g": 3,
    "foods": [{"name": "chicken", "amount": 200, "unit": "g", "calories": 450}],
    "logged_at": "2026-06-07T12:00:00Z",
    "notes": None,
    "confidence": 0.9,
}

_ANTHR_OK = {
    "content": [{"type": "text", "text": json.dumps(_FOOD_EXTRACTION)}]
}


# ── vision_extract ────────────────────────────────────────────────────────────

def test_vision_extract_food_parsed():
    """vision_extract correctly parses a food JSON response from the API."""
    with patch("monitor.inbox_drain.httpx.post", return_value=_mock_http_resp(_ANTHR_OK)):
        result = vision_extract("key", "model", [], "grilled chicken", "food", "2026-06-07T12:00:00Z")

    assert result["description"] == "grilled chicken"
    assert result["confidence"] == 0.9
    assert result["calories"] == 450


def test_vision_extract_strips_fences():
    """_strip_fences removes ```json ... ``` wrappers that Claude sometimes adds."""
    fenced = "```json\n{\"a\": 1}\n```"
    assert _strip_fences(fenced) == '{"a": 1}'


def test_vision_extract_parse_error_returns_error_dict():
    """vision_extract returns {confidence: 0, _error: ...} on bad JSON."""
    bad_response = {"content": [{"type": "text", "text": "not json at all"}]}
    with patch("monitor.inbox_drain.httpx.post", return_value=_mock_http_resp(bad_response)):
        result = vision_extract("key", "model", [], "", "food", "now")

    assert result["confidence"] == 0.0
    assert "_error" in result


def test_vision_extract_api_error_returns_error_dict():
    """vision_extract returns error dict on HTTP 500."""
    err_mock = MagicMock()
    err_mock.raise_for_status.side_effect = httpx.HTTPStatusError(
        "500", request=MagicMock(), response=MagicMock()
    )
    with patch("monitor.inbox_drain.httpx.post", return_value=err_mock):
        result = vision_extract("key", "model", [], "", "food", "now")

    assert result["confidence"] == 0.0
    assert "_error" in result


def test_image_block_shape():
    """_image_block produces correct Anthropic content block shape."""
    block = _image_block(b"fakeimagedata", "image/jpeg")
    assert block["type"] == "image"
    assert block["source"]["type"] == "base64"
    assert block["source"]["media_type"] == "image/jpeg"
    assert len(block["source"]["data"]) > 0


# ── build_clusters ────────────────────────────────────────────────────────────

def test_build_clusters_album():
    items = [_item("a", mgid="mg1"), _item("b", mgid="mg1"), _item("c", mgid="mg1")]
    albums, ungrouped = build_clusters(items)
    assert len(albums) == 1 and len(albums[0]) == 3
    assert ungrouped == []


def test_build_clusters_singleton_mgid_goes_ungrouped():
    items = [_item("a", mgid="mg1")]
    albums, ungrouped = build_clusters(items)
    assert albums == [] and len(ungrouped) == 1


def test_build_clusters_two_unrelated():
    items = [_item("a"), _item("b")]
    albums, ungrouped = build_clusters(items)
    assert albums == [] and len(ungrouped) == 2


# ── content_cluster_ungrouped ─────────────────────────────────────────────────

def test_content_cluster_single_item_no_api_call():
    """Single ungrouped item returns [[item]] without any API call."""
    item = _item("x")
    result = content_cluster_ungrouped("key", "model", [item])
    assert result == [[item]]


def test_content_cluster_api_groups_two():
    """Two items that the API groups together → one cluster of two."""
    items = [_item("a"), _item("b")]
    api_response = {"content": [{"type": "text", "text": json.dumps({"groups": [["a", "b"]]})}]}

    with patch("monitor.inbox_drain.httpx.post", return_value=_mock_http_resp(api_response)):
        result = content_cluster_ungrouped("key", "model", items)

    assert len(result) == 1
    assert {r["id"] for r in result[0]} == {"a", "b"}


def test_content_cluster_api_keeps_separate():
    """Items the API keeps separate → two singletons."""
    items = [_item("a"), _item("b")]
    api_response = {"content": [{"type": "text", "text": json.dumps({"groups": [["a"], ["b"]]})}]}

    with patch("monitor.inbox_drain.httpx.post", return_value=_mock_http_resp(api_response)):
        result = content_cluster_ungrouped("key", "model", items)

    assert len(result) == 2


def test_content_cluster_api_failure_falls_back_to_singletons():
    """API error → fall back to singletons, never raise."""
    items = [_item("a"), _item("b")]
    with patch("monitor.inbox_drain.httpx.post", side_effect=Exception("timeout")):
        result = content_cluster_ungrouped("key", "model", items)
    assert len(result) == 2


# ── merge_caption ─────────────────────────────────────────────────────────────

def test_merge_caption_deduplicates():
    rows = [_item("a", caption="chicken"), _item("b", caption="chicken"), _item("c", caption="salad")]
    assert merge_caption(rows) == "chicken | salad"


def test_merge_caption_empty():
    rows = [_item("a", caption=""), _item("b", caption=None)]
    assert merge_caption(rows) == ""


# ── inbox_drain: write_food (confidence routing) ──────────────────────────────

def test_write_food_high_confidence_routes_to_prod():
    """confidence >= threshold → RPC returns inserted → rows marked done."""
    from monitor.inbox_drain import write_food

    prod_result = {"id": "food-uuid", "status": "inserted"}
    db = _db([
        (200, prod_result),    # rpc call
        (200, []),             # mark_rows PATCH
    ])
    rows = [_item("r1")]
    result = write_food(db, rows, rows[0]["profile_id"],
                        {"meal_type": "lunch", "description": "grilled chicken"},
                        confidence=0.9, raw_text="grilled chicken")
    assert result.get("status") == "inserted"


def test_write_food_low_confidence_routes_to_staging():
    """confidence < threshold → RPC returns staged → rows marked staged."""
    from monitor.inbox_drain import write_food

    stg_result = {"id": "stg-uuid", "status": "staged"}
    db = _db([
        (200, stg_result),
        (200, []),
    ])
    rows = [_item("r1")]
    result = write_food(db, rows, rows[0]["profile_id"],
                        {"meal_type": "unknown", "description": "blurry food"},
                        confidence=0.3, raw_text="blurry")
    assert result.get("status") == "staged"


# ── inbox_drain: write_biomarker / write_supplement unresolved-UUID paths ─────

def test_write_biomarker_missing_metric_id_does_not_raise():
    """When lookup_metric finds no match, extracted has no metric_definition_id.
    write_biomarker must not KeyError — it should pass None and let the RPC decide."""
    from monitor.inbox_drain import write_biomarker

    rpc_result = {"id": "stg-uuid", "status": "staged"}
    db = _db([
        (200, rpc_result),
        (200, []),
    ])
    rows = [_item("r1", kind="lab")]
    result = write_biomarker(db, rows, rows[0]["profile_id"],
                             {"extracted_name": "Glucose", "value": 95, "unit": "mg/dL"},
                             confidence=0.8, raw_text="Glucose 95")
    assert isinstance(result, dict)


def test_write_supplement_missing_supplement_id_does_not_raise():
    """When lookup_supplement_by_name finds no match, extracted has no supplement_id.
    write_supplement must not KeyError — it should pass None and let the RPC decide."""
    from monitor.inbox_drain import write_supplement

    rpc_result = {"id": "stg-uuid", "status": "staged"}
    db = _db([
        (200, rpc_result),
        (200, []),
    ])
    rows = [_item("r1", kind="supplement")]
    result = write_supplement(db, rows, rows[0]["profile_id"],
                              {"name": "Unknown Herb", "dose_amount": 500, "dose_unit": "mg"},
                              confidence=0.6, raw_text="Unknown Herb 500mg")
    assert isinstance(result, dict)


# ── compose_confirmation (minor framing) ──────────────────────────────────────

_DEFICIT_WORDS = ["low", "poor", "bad", "deficit", "restrict", "not enough", "missing"]


def test_minor_food_confirmation_no_deficit_language():
    msg = compose_confirmation("food", "inserted", {"description": "salad", "calories": 350}, is_minor=True)
    for w in _DEFICIT_WORDS:
        assert w not in msg.lower(), f"deficit word '{w}' in: {msg}"


def test_minor_staged_confirmation_no_deficit_language():
    msg = compose_confirmation("food", "staged", {}, is_minor=True)
    for w in _DEFICIT_WORDS:
        assert w not in msg.lower(), f"deficit word '{w}' in: {msg}"


def test_minor_lab_confirmation_no_deficit_language():
    msg = compose_confirmation("lab", "inserted", {"biomarkers": [{}, {}]}, is_minor=True)
    for w in _DEFICIT_WORDS:
        assert w not in msg.lower(), f"deficit word '{w}' in: {msg}"


# ── run_once ──────────────────────────────────────────────────────────────────

def _make_db_for_run_once(extra_responses: list[tuple[int, Any]] | None = None) -> DbRest:
    """Mock DB that returns: identities, inbox items, claim row, then extras.

    run_once receives cfg directly — it never calls get_config — so the first
    DB call is telegram_identities.
    """
    identities = [{"chat_id": 99, "is_minor": False}]
    inbox = [_item("r1", caption="chicken")]
    claim_ok = [{"id": "r1", "status": "processing"}]  # claim returns 1 row

    base = [
        (200, identities),  # telegram_identities
        (200, inbox),       # fetch_settled
        (200, claim_ok),    # claim r1
    ]
    return _db(base + (extra_responses or []))


def test_run_once_written():
    """High-confidence extraction → written=1, staged=0, failed=0."""
    db = _make_db_for_run_once([
        (200, {"id": "food-uuid", "status": "inserted"}),  # rpc maintainer_ingest_food
        (200, []),           # mark_rows PATCH
    ])

    with patch("monitor.inbox_drain.vision_extract", return_value=_FOOD_EXTRACTION), \
         patch("monitor.inbox_drain.telegram_send"):
        summary = run_once(db, {
            "push.inbox_settle_sec": "0",
            "ingest.confidence_threshold": "0.7",
            "drain.vision_model": '"claude-sonnet-4-6"',
        }, "api-key", "tg-token")

    assert summary["written"] == 1
    assert summary["staged"] == 0
    assert summary["failed"] == 0


def test_run_once_staged():
    """Low-confidence extraction → staged=1."""
    low_conf = {**_FOOD_EXTRACTION, "confidence": 0.3}
    db = _make_db_for_run_once([
        (200, {"id": "stg-uuid", "status": "staged"}),
        (200, []),
    ])

    with patch("monitor.inbox_drain.vision_extract", return_value=low_conf), \
         patch("monitor.inbox_drain.telegram_send"):
        summary = run_once(db, {
            "push.inbox_settle_sec": "0",
            "ingest.confidence_threshold": "0.7",
            "drain.vision_model": '"claude-sonnet-4-6"',
        }, "api-key", "tg-token")

    assert summary["staged"] == 1
    assert summary["written"] == 0
    assert summary["failed"] == 0


def test_run_once_failed_on_vision_error():
    """Vision extraction error → failed=1, errors non-empty."""
    error_extraction = {"confidence": 0.0, "_error": "api timeout"}
    db = _make_db_for_run_once([
        (200, []),  # mark_rows PATCH (mark failed)
    ])

    with patch("monitor.inbox_drain.vision_extract", return_value=error_extraction), \
         patch("monitor.inbox_drain.telegram_send"):
        summary = run_once(db, {
            "push.inbox_settle_sec": "0",
            "ingest.confidence_threshold": "0.7",
            "drain.vision_model": '"claude-sonnet-4-6"',
        }, "api-key", "tg-token")

    assert summary["failed"] == 1
    assert len(summary["errors"]) == 1


def test_run_once_empty_inbox():
    """Empty inbox → summary zeros, no API calls."""
    db = _db([
        (200, []),  # identities (run_once receives cfg directly, first call is identities)
        (200, []),  # fetch_settled returns empty
    ])

    summary = run_once(db, {
        "push.inbox_settle_sec": "90",
        "ingest.confidence_threshold": "0.7",
        "drain.vision_model": '"claude-sonnet-4-6"',
    }, "api-key", "tg-token")

    assert summary["fetched"] == 0
    assert summary["clustered"] == 0
    assert summary["written"] == 0
