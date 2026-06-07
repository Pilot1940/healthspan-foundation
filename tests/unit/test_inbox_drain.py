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
    _totals_line,
    biomarker_is_complete,
    build_clusters,
    compose_confirmation,
    content_cluster_ungrouped,
    fetch_today_food_totals,
    fetch_today_supplement_counts,
    food_is_complete,
    merge_caption,
    run_once,
    supplement_is_complete,
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


def test_vision_extract_parse_error_returns_stage_reason():
    """Bad JSON → {confidence: 0, _stage_reason: ...} so the item is STAGED, not failed."""
    bad_response = {"content": [{"type": "text", "text": "not json at all"}]}
    with patch("monitor.inbox_drain.httpx.post", return_value=_mock_http_resp(bad_response)):
        result = vision_extract("key", "model", [], "", "food", "now")

    assert result["confidence"] == 0.0
    assert result.get("_stage_reason") == "vision returned no parseable extraction"
    assert "_error" not in result


def test_vision_extract_empty_body_returns_stage_reason():
    """Empty/shapeless response body → _stage_reason (fail-safe), never a crash."""
    empty_response = {}  # no "content" key
    with patch("monitor.inbox_drain.httpx.post", return_value=_mock_http_resp(empty_response)):
        result = vision_extract("key", "model", [], "", "food", "now")

    assert result["confidence"] == 0.0
    assert result.get("_stage_reason") == "vision returned no parseable extraction"


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

def test_write_food_forwards_force_stage_false():
    """write_food(force_stage=False) sends p_force_stage=False — RPC decides prod/stage."""
    from monitor.inbox_drain import write_food

    db, captured = _db_capture([
        (200, {"id": "food-uuid", "status": "inserted"}),  # rpc call
        (200, []),                                          # mark_rows PATCH
    ])
    rows = [_item("r1")]
    result = write_food(db, rows, rows[0]["profile_id"],
                        {"meal_type": "lunch", "description": "grilled chicken"},
                        confidence=0.9, raw_text="grilled chicken", force_stage=False)
    assert result.get("status") == "inserted"
    rpc = next(c for c in captured if "maintainer_ingest_food" in c["url"])
    assert rpc["body"]["p_force_stage"] is False


def test_write_food_forwards_force_stage_true():
    """write_food(force_stage=True) sends p_force_stage=True — forces staging."""
    from monitor.inbox_drain import write_food

    db, captured = _db_capture([
        (200, {"id": "stg-uuid", "status": "staged"}),
        (200, []),
    ])
    rows = [_item("r1")]
    result = write_food(db, rows, rows[0]["profile_id"],
                        {"meal_type": "unknown", "description": "blurry food"},
                        confidence=0.9, raw_text="blurry", force_stage=True)
    assert result.get("status") == "staged"
    rpc = next(c for c in captured if "maintainer_ingest_food" in c["url"])
    assert rpc["body"]["p_force_stage"] is True


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

_PC_PROFILE_ID = "21f69003-46f8-4e1c-a928-b1f694ce4aff"


def _make_db_for_run_once(extra_responses: list[tuple[int, Any]] | None = None) -> DbRest:
    """Mock DB that returns: identities, inbox items, claim row, then extras.

    run_once receives cfg directly — it never calls get_config — so the first
    DB call is telegram_identities.
    """
    identities = [{"chat_id": 99, "is_minor": False,
                   "profile_id": _PC_PROFILE_ID, "display_name": "PC"}]
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
         patch("monitor.inbox_drain.telegram_send"), \
         patch("monitor.inbox_drain.fetch_today_food_totals", return_value={}), \
         patch("monitor.inbox_drain.fetch_today_supplement_counts", return_value={}):
        summary = run_once(db, {
            "push.inbox_settle_sec": "0",
            "ingest.confidence_threshold": "0.7",
            "drain.vision_model": '"claude-sonnet-4-6"',
        }, "api-key", "tg-token")

    assert summary["written"] == 1
    assert summary["staged"] == 0
    assert summary["failed"] == 0


def test_run_once_staged():
    """Incomplete extraction (no calories/macros) → staged=1, regardless of confidence."""
    incomplete = {**_FOOD_EXTRACTION, "confidence": 0.95,
                  "calories": None, "protein_g": None, "carbs_g": None,
                  "fat_g": None, "fiber_g": None}
    db = _make_db_for_run_once([
        (200, {"id": "stg-uuid", "status": "staged"}),
        (200, []),
    ])

    with patch("monitor.inbox_drain.vision_extract", return_value=incomplete), \
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


# ── completeness gate (Bug 3) ─────────────────────────────────────────────────

_COMPLETE_SHAKE = {
    "meal_type": "snack",
    "description": "protein shake",
    "calories": 320,
    "protein_g": 40,
    "carbs_g": 18,
    "fat_g": 6,
    "fiber_g": 2,
    "confidence": 0.5,  # low self-reported confidence — must NOT block a complete extraction
}

_BARE_CAPTION = {
    "meal_type": "unknown",
    "description": "had lunch",
    "calories": None,
    "protein_g": None,
    "carbs_g": None,
    "fat_g": None,
    "confidence": 0.95,  # high confidence but incomplete — must stage
}


def test_food_is_complete_full_shake_true():
    """A full-macro shake is complete even at 0.5 self-reported confidence."""
    assert food_is_complete(_COMPLETE_SHAKE) is True


def test_food_is_complete_bare_caption_false():
    """No calories / no macros → not complete, regardless of high confidence."""
    assert food_is_complete(_BARE_CAPTION) is False


def test_food_is_complete_calories_only_false():
    """Calories but no macro → not complete."""
    assert food_is_complete({"description": "rice", "calories": 200}) is False


def test_food_is_complete_no_description_false():
    """Macros + calories but no description → not complete."""
    assert food_is_complete({"calories": 200, "protein_g": 10}) is False


def _db_capture(responses: list[tuple[int, Any]]) -> tuple[DbRest, list[dict]]:
    """Like _db but records each request's parsed JSON body in the returned list."""
    captured: list[dict] = []
    idx = 0

    def _handler(request: httpx.Request) -> httpx.Response:
        nonlocal idx
        try:
            captured.append({"url": str(request.url), "body": json.loads(request.content or b"{}")})
        except Exception:
            captured.append({"url": str(request.url), "body": None})
        status, body = responses[idx % len(responses)]
        idx += 1
        content = json.dumps(body).encode()
        return httpx.Response(status, content=content,
                              headers={"content-type": "application/json"})

    client = httpx.Client(transport=httpx.MockTransport(_handler))
    db = DbRest.__new__(DbRest)
    db._base = "https://test.supabase.co"
    db._anon_key = "anon"
    db._jwt = "jwt"
    db._client = client
    return db, captured


def test_completeness_gate_complete_shake_autowrites():
    """The 0.5-confidence full-macro shake must be WRITTEN (force_stage=False)."""
    db, captured = _db_capture([
        (200, {"id": "food-uuid", "status": "inserted"}),  # rpc
        (200, []),                                          # mark_rows
    ])
    from monitor.inbox_drain import _process_cluster
    summary = {"fetched": 0, "clustered": 0, "written": 0, "staged": 0, "failed": 0, "errors": []}
    cluster = [_item("r1", caption="protein shake")]

    with patch("monitor.inbox_drain.vision_extract", return_value=_COMPLETE_SHAKE), \
         patch("monitor.inbox_drain.telegram_send"):
        _process_cluster(db, cluster, "key", "model", "now", 0.7, {}, "tok", summary)

    assert summary["written"] == 1
    assert summary["staged"] == 0
    rpc_call = next(c for c in captured if "maintainer_ingest_food" in c["url"])
    assert rpc_call["body"]["p_force_stage"] is False


def test_completeness_gate_bare_caption_stages():
    """A bare caption (no calories/macros) must STAGE (force_stage=True)."""
    db, captured = _db_capture([
        (200, {"id": "stg-uuid", "status": "staged"}),  # rpc
        (200, []),                                       # mark_rows
    ])
    from monitor.inbox_drain import _process_cluster
    summary = {"fetched": 0, "clustered": 0, "written": 0, "staged": 0, "failed": 0, "errors": []}
    cluster = [_item("r1", caption="had lunch")]

    with patch("monitor.inbox_drain.vision_extract", return_value=_BARE_CAPTION), \
         patch("monitor.inbox_drain.telegram_send"):
        _process_cluster(db, cluster, "key", "model", "now", 0.7, {}, "tok", summary)

    assert summary["staged"] == 1
    assert summary["written"] == 0
    rpc_call = next(c for c in captured if "maintainer_ingest_food" in c["url"])
    assert rpc_call["body"]["p_force_stage"] is True


# ── list-of-foods write path (Bug 1) ──────────────────────────────────────────

def test_food_list_writes_each_item():
    """A list of foods (multi-item text) writes one RPC per item, marks cluster once."""
    db, captured = _db_capture([
        (200, {"id": "f1", "status": "inserted"}),  # rpc item 1
        (200, {"id": "f2", "status": "inserted"}),  # rpc item 2
        (200, {"id": "f3", "status": "inserted"}),  # rpc item 3
        (200, []),                                   # mark_rows (once)
    ])
    from monitor.inbox_drain import _process_cluster
    summary = {"fetched": 0, "clustered": 0, "written": 0, "staged": 0, "failed": 0, "errors": []}
    cluster = [_item("r1", caption="shake + fish + rice")]

    food_list = [
        {"description": "protein shake", "calories": 320, "protein_g": 40, "confidence": 0.9},
        {"description": "grilled fish", "calories": 400, "protein_g": 60, "confidence": 0.9},
        {"description": "bowl rice", "calories": 200, "carbs_g": 45, "confidence": 0.9},
    ]

    with patch("monitor.inbox_drain.vision_extract", return_value=food_list), \
         patch("monitor.inbox_drain.telegram_send"):
        _process_cluster(db, cluster, "key", "model", "now", 0.7, {}, "tok", summary)

    rpc_calls = [c for c in captured if "maintainer_ingest_food" in c["url"]]
    assert len(rpc_calls) == 3
    assert summary["written"] == 1  # cluster counts once, all items inserted
    assert summary["failed"] == 0
    # one mark_rows PATCH on media_inbox
    patches = [c for c in captured if "media_inbox" in c["url"]]
    assert len(patches) == 1


def test_food_list_mixed_status_stages_cluster():
    """If any item in a list stages (none fail), the whole cluster is staged."""
    db, captured = _db_capture([
        (200, {"id": "f1", "status": "inserted"}),  # complete item
        (200, {"id": "f2", "status": "staged"}),    # incomplete item → staged
        (200, []),                                   # mark_rows
    ])
    from monitor.inbox_drain import _process_cluster
    summary = {"fetched": 0, "clustered": 0, "written": 0, "staged": 0, "failed": 0, "errors": []}
    cluster = [_item("r1")]

    food_list = [
        {"description": "shake", "calories": 320, "protein_g": 40, "confidence": 0.9},
        {"description": "mystery side", "calories": None, "confidence": 0.9},
    ]

    with patch("monitor.inbox_drain.vision_extract", return_value=food_list), \
         patch("monitor.inbox_drain.telegram_send"):
        _process_cluster(db, cluster, "key", "model", "now", 0.7, {}, "tok", summary)

    assert summary["staged"] == 1
    assert summary["written"] == 0
    # second item incomplete → force_stage True
    rpc_calls = [c for c in captured if "maintainer_ingest_food" in c["url"]]
    assert rpc_calls[0]["body"]["p_force_stage"] is False
    assert rpc_calls[1]["body"]["p_force_stage"] is True


# ── empty-vision → staged (Bug 2) ─────────────────────────────────────────────

def test_empty_vision_stages_not_fails():
    """_stage_reason from vision_extract → cluster STAGED, never failed."""
    db, captured = _db_capture([
        (200, []),  # mark_rows PATCH (staged)
    ])
    from monitor.inbox_drain import _process_cluster
    summary = {"fetched": 0, "clustered": 0, "written": 0, "staged": 0, "failed": 0, "errors": []}
    cluster = [_item("r1")]

    stage_result = {"confidence": 0.0, "_stage_reason": "vision returned no parseable extraction"}
    with patch("monitor.inbox_drain.vision_extract", return_value=stage_result), \
         patch("monitor.inbox_drain.telegram_send"):
        _process_cluster(db, cluster, "key", "model", "now", 0.7, {}, "tok", summary)

    assert summary["staged"] == 1
    assert summary["failed"] == 0
    # no RPC call — staged before any write
    assert not [c for c in captured if "rpc" in c["url"]]


def test_unknown_kind_forces_stage_not_prod():
    """A workout/unknown item must STAGE (force_stage=True), never auto-write to prod."""
    db, captured = _db_capture([
        (200, {"id": "stg-uuid", "status": "staged"}),  # rpc
        (200, []),                                       # mark_rows
    ])
    from monitor.inbox_drain import _process_cluster
    summary = {"fetched": 0, "clustered": 0, "written": 0, "staged": 0, "failed": 0, "errors": []}
    cluster = [_item("r1", kind="workout", caption="ran 5k")]

    # extraction with no kind re-dispatch → falls to the else branch
    extraction = {"confidence": 0.9, "notes": "ran 5k"}
    with patch("monitor.inbox_drain.vision_extract", return_value=extraction), \
         patch("monitor.inbox_drain.telegram_send"):
        _process_cluster(db, cluster, "key", "model", "now", 0.7, {}, "tok", summary)

    assert summary["staged"] == 1
    assert summary["written"] == 0
    rpc = next(c for c in captured if "maintainer_ingest_food" in c["url"])
    assert rpc["body"]["p_force_stage"] is True


# ── fetch_today_food_totals ───────────────────────────────────────────────────

def test_fetch_today_food_totals_sums_rows():
    """Two food_logs rows → correct kcal/protein sum."""
    rows = [
        {"calories": 450, "protein_g": 40, "carbs_g": 20, "fat_g": 12},
        {"calories": 320, "protein_g": 30, "carbs_g": 18, "fat_g": 6},
    ]
    db = _db([(200, rows)])
    result = fetch_today_food_totals(db, _PC_PROFILE_ID, "2026-06-07")
    assert result["kcal"] == 770
    assert result["protein_g"] == 70.0
    assert result["meals"] == 2


def test_fetch_today_food_totals_empty_returns_zeros():
    """No food_logs rows today → kcal=0, meals=0 (not empty dict)."""
    db = _db([(200, [])])
    result = fetch_today_food_totals(db, _PC_PROFILE_ID, "2026-06-07")
    assert result["kcal"] == 0
    assert result["meals"] == 0


def test_fetch_today_food_totals_null_calories_skipped():
    """NULL calories (row with no calorie data) → treated as 0."""
    rows = [{"calories": None, "protein_g": 40, "carbs_g": 0, "fat_g": 0}]
    db = _db([(200, rows)])
    result = fetch_today_food_totals(db, _PC_PROFILE_ID, "2026-06-07")
    assert result["kcal"] == 0
    assert result["meals"] == 1


def test_fetch_today_food_totals_db_error_returns_empty():
    """DB error (non-200) → returns {} silently (best-effort, never crashes drain)."""
    db = _db([(500, {"message": "internal error"})])
    result = fetch_today_food_totals(db, _PC_PROFILE_ID, "2026-06-07")
    assert result == {}


# ── fetch_today_supplement_counts ────────────────────────────────────────────

def test_fetch_today_supplement_counts_taken_and_total():
    """3 active regimens, 2 taken today → {taken: 2, total: 3}."""
    ids = ["s1", "s2", "s3"]
    regimens = [{"supplement_id": sid} for sid in ids]
    intakes = [{"supplement_id": "s1"}, {"supplement_id": "s2"}]  # s3 not taken
    db = _db([(200, regimens), (200, intakes)])
    result = fetch_today_supplement_counts(db, _PC_PROFILE_ID, "2026-06-07")
    assert result == {"taken": 2, "total": 3}


def test_fetch_today_supplement_counts_no_regimens_returns_empty():
    """No active regimens → {} (nothing to show)."""
    db = _db([(200, [])])
    result = fetch_today_supplement_counts(db, _PC_PROFILE_ID, "2026-06-07")
    assert result == {}


def test_fetch_today_supplement_counts_none_taken():
    """Regimens exist but nothing taken today → taken=0."""
    db = _db([(200, [{"supplement_id": "s1"}, {"supplement_id": "s2"}]), (200, [])])
    result = fetch_today_supplement_counts(db, _PC_PROFILE_ID, "2026-06-07")
    assert result == {"taken": 0, "total": 2}


# ── _totals_line ─────────────────────────────────────────────────────────────

def test_totals_line_adult_with_targets():
    """Adult with targets: shows kcal pct, protein fraction, supp count."""
    totals = {"kcal": 950, "protein_g": 65.0, "meals": 2}
    supp = {"taken": 4, "total": 16}
    line = _totals_line(totals, supp, target_cal=2100, target_protein_g=180, is_minor=False)
    assert "950 / 2100 kcal" in line
    assert "45%" in line
    assert "65.0 / 180g protein" in line
    assert "Supps 4/16" in line


def test_totals_line_adult_no_targets():
    """Adult without targets: shows raw kcal and protein, still useful."""
    totals = {"kcal": 800, "protein_g": 55.0, "meals": 2}
    line = _totals_line(totals, {}, target_cal=None, target_protein_g=None, is_minor=False)
    assert "800 kcal" in line
    assert "55.0g protein" in line
    assert "Supps" not in line


def test_totals_line_minor_no_deficit_language():
    """Minor: shows totals, no target percentage, no deficit/restriction words."""
    totals = {"kcal": 1200, "protein_g": 45.0, "meals": 3}
    line = _totals_line(totals, {}, target_cal=2400, target_protein_g=80, is_minor=True)
    assert "1200 kcal" in line
    assert "45.0g protein" in line
    # No adult-style percentage or fraction
    assert "%" not in line
    assert "/ 2400" not in line
    # No deficit/restriction words
    for w in ("deficit", "restrict", "not enough", "low", "poor", "missing"):
        assert w not in line.lower(), f"deficit word '{w}' in: {line}"


def test_totals_line_minor_low_intake_positive_framing():
    """Minor with very low kcal: growth/performance framing only — never 'not enough'."""
    totals = {"kcal": 400, "protein_g": 20.0, "meals": 1}  # 400 < 60% of 2400
    line = _totals_line(totals, {}, target_cal=2400, target_protein_g=80, is_minor=True)
    # Must encourage, not shame
    assert any(w in line.lower() for w in ("fuel", "strong", "energy", "💪")), (
        f"expected positive framing in: {line}"
    )
    for w in ("deficit", "restrict", "not enough", "low", "poor"):
        assert w not in line.lower(), f"deficit word '{w}' in: {line}"


def test_totals_line_empty_totals_returns_empty_string():
    """Empty totals dict (DB error) → '' so nothing gets appended to the message."""
    assert _totals_line({}, {}, 2100, 180, is_minor=False) == ""


# ── end-of-run summary ────────────────────────────────────────────────────────

def test_run_once_sends_end_summary_written():
    """After a successful write, run_once sends a 'Done — N logged' summary."""
    db = _make_db_for_run_once([
        (200, {"id": "food-uuid", "status": "inserted"}),
        (200, []),  # mark_rows
    ])
    with patch("monitor.inbox_drain.vision_extract", return_value=_FOOD_EXTRACTION), \
         patch("monitor.inbox_drain.fetch_today_food_totals", return_value={}), \
         patch("monitor.inbox_drain.fetch_today_supplement_counts", return_value={}), \
         patch("monitor.inbox_drain.telegram_send") as mock_tg:
        run_once(db, {
            "push.inbox_settle_sec": "0",
            "ingest.confidence_threshold": "0.7",
            "drain.vision_model": '"claude-sonnet-4-6"',
        }, "api-key", "tg-token")

    calls = [str(c.args) for c in mock_tg.call_args_list]
    summary_calls = [c for c in calls if "Done" in c]
    assert len(summary_calls) == 1
    assert "1 logged" in summary_calls[0]
    assert "to review" not in summary_calls[0]


def test_run_once_sends_end_summary_staged():
    """A staged item → summary says 'to review', not 'logged'."""
    incomplete = {**_FOOD_EXTRACTION, "calories": None, "protein_g": None,
                  "carbs_g": None, "fat_g": None}
    db = _make_db_for_run_once([
        (200, {"id": "stg-uuid", "status": "staged"}),
        (200, []),
    ])
    with patch("monitor.inbox_drain.vision_extract", return_value=incomplete), \
         patch("monitor.inbox_drain.telegram_send") as mock_tg:
        run_once(db, {
            "push.inbox_settle_sec": "0",
            "ingest.confidence_threshold": "0.7",
            "drain.vision_model": '"claude-sonnet-4-6"',
        }, "api-key", "tg-token")

    calls = [str(c.args) for c in mock_tg.call_args_list]
    summary_calls = [c for c in calls if "Done" in c]
    assert len(summary_calls) == 1
    assert "1 to review" in summary_calls[0]
    assert "logged" not in summary_calls[0]


def test_run_once_no_summary_on_failure_only():
    """A hard failure (vision _error) with no writes or stages → no 'Done' message."""
    error_extraction = {"confidence": 0.0, "_error": "api timeout"}
    db = _make_db_for_run_once([(200, [])])  # mark_rows

    with patch("monitor.inbox_drain.vision_extract", return_value=error_extraction), \
         patch("monitor.inbox_drain.telegram_send") as mock_tg:
        run_once(db, {
            "push.inbox_settle_sec": "0",
            "ingest.confidence_threshold": "0.7",
            "drain.vision_model": '"claude-sonnet-4-6"',
        }, "api-key", "tg-token")

    calls = [str(c.args) for c in mock_tg.call_args_list]
    assert not any("Done" in c for c in calls)


# ── totals appended to food confirmation ─────────────────────────────────────

def test_process_cluster_appends_totals_on_write():
    """After a food write, the confirmation includes the totals line."""
    db, captured = _db_capture([
        (200, {"id": "food-uuid", "status": "inserted"}),
        (200, []),  # mark_rows
    ])
    from monitor.inbox_drain import _process_cluster
    summary = {"fetched": 0, "clustered": 0, "written": 0, "staged": 0, "failed": 0, "errors": []}
    cluster = [_item("r1", caption="protein shake")]
    totals = {"kcal": 950, "protein_g": 65.0, "meals": 2}
    ctx = {"targets": {"daily_calories": 2100, "protein_g": 180}}

    with patch("monitor.inbox_drain.vision_extract", return_value=_COMPLETE_SHAKE), \
         patch("monitor.inbox_drain.fetch_today_food_totals", return_value=totals), \
         patch("monitor.inbox_drain.fetch_today_supplement_counts", return_value={"taken": 4, "total": 16}), \
         patch("monitor.inbox_drain.telegram_send") as mock_tg:
        _process_cluster(db, cluster, "key", "model", "now", 0.7, {},
                         "tok", summary,
                         profile_ctx={cluster[0]["profile_id"]: ctx},
                         today="2026-06-07")

    msg = mock_tg.call_args.args[2]
    assert "Today:" in msg
    assert "950 / 2100 kcal" in msg
    assert "Supps 4/16" in msg


def test_process_cluster_no_totals_on_staged():
    """Staged item → no totals line appended (food not in food_logs yet)."""
    db, captured = _db_capture([
        (200, {"id": "stg-uuid", "status": "staged"}),
        (200, []),
    ])
    from monitor.inbox_drain import _process_cluster
    summary = {"fetched": 0, "clustered": 0, "written": 0, "staged": 0, "failed": 0, "errors": []}
    cluster = [_item("r1")]
    incomplete = {**_FOOD_EXTRACTION, "calories": None, "protein_g": None,
                  "carbs_g": None, "fat_g": None}

    with patch("monitor.inbox_drain.vision_extract", return_value=incomplete), \
         patch("monitor.inbox_drain.fetch_today_food_totals") as mock_totals, \
         patch("monitor.inbox_drain.telegram_send") as mock_tg:
        _process_cluster(db, cluster, "key", "model", "now", 0.7, {},
                         "tok", summary,
                         profile_ctx={}, today="2026-06-07")

    mock_totals.assert_not_called()
    msg = mock_tg.call_args.args[2]
    assert "Today:" not in msg


# ── persist stage_reason ──────────────────────────────────────────────────────

def test_mark_rows_includes_stage_reason_in_patch():
    """mark_rows with stage_reason writes it to the PATCH body."""
    from monitor.inbox_drain import mark_rows

    db, captured = _db_capture([(200, [])])
    mark_rows(db, ["r1", "r2"], "staged", stage_reason="incomplete: missing calories or macros")

    patch_call = next(c for c in captured if "media_inbox" in c["url"])
    assert patch_call["body"]["stage_reason"] == "incomplete: missing calories or macros"
    assert patch_call["body"]["status"] == "staged"


def test_mark_rows_omits_stage_reason_when_none():
    """mark_rows without stage_reason does not include the key in the PATCH body."""
    from monitor.inbox_drain import mark_rows

    db, captured = _db_capture([(200, [])])
    mark_rows(db, ["r1"], "done")

    patch_call = next(c for c in captured if "media_inbox" in c["url"])
    assert "stage_reason" not in patch_call["body"]


def test_stage_reason_vision_parse_failure():
    """_stage_reason from vision_extract → stage_reason written to media_inbox."""
    db, captured = _db_capture([
        (200, []),  # mark_rows PATCH
    ])
    from monitor.inbox_drain import _process_cluster
    summary = {"fetched": 0, "clustered": 0, "written": 0, "staged": 0, "failed": 0, "errors": []}
    cluster = [_item("r1")]

    stage_result = {"confidence": 0.0, "_stage_reason": "vision returned no parseable extraction"}
    with patch("monitor.inbox_drain.vision_extract", return_value=stage_result), \
         patch("monitor.inbox_drain.telegram_send"):
        _process_cluster(db, cluster, "key", "model", "now", 0.7, {}, "tok", summary)

    patch_call = next(c for c in captured if "media_inbox" in c["url"])
    assert patch_call["body"]["status"] == "staged"
    assert patch_call["body"]["stage_reason"] == "vision returned no parseable extraction"


def test_stage_reason_vision_error():
    """_error from vision_extract → error message written as stage_reason on failed media_inbox."""
    db, captured = _db_capture([(200, [])])
    from monitor.inbox_drain import _process_cluster
    summary = {"fetched": 0, "clustered": 0, "written": 0, "staged": 0, "failed": 0, "errors": []}
    cluster = [_item("r1")]

    with patch("monitor.inbox_drain.vision_extract",
               return_value={"confidence": 0.0, "_error": "connection timeout"}), \
         patch("monitor.inbox_drain.telegram_send"):
        _process_cluster(db, cluster, "key", "model", "now", 0.7, {}, "tok", summary)

    patch_call = next(c for c in captured if "media_inbox" in c["url"])
    assert patch_call["body"]["status"] == "failed"
    assert patch_call["body"]["stage_reason"] == "connection timeout"


def test_stage_reason_incomplete_food():
    """Incomplete food extraction → p_stage_reason sent to RPC and propagated to media_inbox."""
    # RPC echoes back the stage_reason (as DB would)
    db, captured = _db_capture([
        (200, {"id": "stg-uuid", "status": "staged",
               "stage_reason": "incomplete: missing calories or macros"}),
        (200, []),  # mark_rows
    ])
    from monitor.inbox_drain import _process_cluster
    summary = {"fetched": 0, "clustered": 0, "written": 0, "staged": 0, "failed": 0, "errors": []}
    cluster = [_item("r1", caption="had lunch")]

    with patch("monitor.inbox_drain.vision_extract", return_value=_BARE_CAPTION), \
         patch("monitor.inbox_drain.telegram_send"):
        _process_cluster(db, cluster, "key", "model", "now", 0.7, {}, "tok", summary)

    rpc_call = next(c for c in captured if "maintainer_ingest_food" in c["url"])
    assert rpc_call["body"]["p_stage_reason"] == "incomplete: missing calories or macros"

    patch_call = next(c for c in captured if "media_inbox" in c["url"])
    assert patch_call["body"]["status"] == "staged"
    assert patch_call["body"]["stage_reason"] == "incomplete: missing calories or macros"


def test_stage_reason_implausible_kcal_from_rpc():
    """DB-side kcal gate fires: RPC returns stage_reason → propagated to media_inbox."""
    kcal_reason = "implausible calories: 15000 kcal (must be 25–12000)"
    db, captured = _db_capture([
        (200, {"id": "stg-uuid", "status": "staged", "stage_reason": kcal_reason}),
        (200, []),
    ])
    from monitor.inbox_drain import _process_cluster
    summary = {"fetched": 0, "clustered": 0, "written": 0, "staged": 0, "failed": 0, "errors": []}
    # High-kcal but otherwise complete extraction — p_force_stage=False; DB gate fires
    implausible = {**_FOOD_EXTRACTION, "calories": 15000}
    cluster = [_item("r1", caption="mega meal")]

    with patch("monitor.inbox_drain.vision_extract", return_value=implausible), \
         patch("monitor.inbox_drain.telegram_send"):
        _process_cluster(db, cluster, "key", "model", "now", 0.7, {}, "tok", summary)

    rpc_call = next(c for c in captured if "maintainer_ingest_food" in c["url"])
    # p_force_stage must be False — complete extraction, Python doesn't force stage
    assert rpc_call["body"]["p_force_stage"] is False
    # p_stage_reason is None (Python didn't know it would be staged)
    assert rpc_call["body"]["p_stage_reason"] is None

    patch_call = next(c for c in captured if "media_inbox" in c["url"])
    assert patch_call["body"]["status"] == "staged"
    assert patch_call["body"]["stage_reason"] == kcal_reason


def test_stage_reason_unknown_kind():
    """workout/unknown kind → stage_reason includes the kind name."""
    db, captured = _db_capture([
        (200, {"id": "stg-uuid", "status": "staged",
               "stage_reason": "unknown kind: workout"}),
        (200, []),
    ])
    from monitor.inbox_drain import _process_cluster
    summary = {"fetched": 0, "clustered": 0, "written": 0, "staged": 0, "failed": 0, "errors": []}
    cluster = [_item("r1", kind="workout", caption="ran 5k")]

    with patch("monitor.inbox_drain.vision_extract",
               return_value={"confidence": 0.9, "notes": "ran 5k"}), \
         patch("monitor.inbox_drain.telegram_send"):
        _process_cluster(db, cluster, "key", "model", "now", 0.7, {}, "tok", summary)

    rpc_call = next(c for c in captured if "maintainer_ingest_food" in c["url"])
    assert rpc_call["body"]["p_stage_reason"] == "unknown kind: workout"

    patch_call = next(c for c in captured if "media_inbox" in c["url"])
    assert patch_call["body"]["stage_reason"] == "unknown kind: workout"


def test_stage_reason_absent_on_successful_write():
    """Successful write (inserted) → stage_reason NOT present in media_inbox PATCH."""
    db, captured = _db_capture([
        (200, {"id": "food-uuid", "status": "inserted"}),
        (200, []),
    ])
    from monitor.inbox_drain import _process_cluster
    summary = {"fetched": 0, "clustered": 0, "written": 0, "staged": 0, "failed": 0, "errors": []}
    cluster = [_item("r1", caption="protein shake")]

    with patch("monitor.inbox_drain.vision_extract", return_value=_COMPLETE_SHAKE), \
         patch("monitor.inbox_drain.telegram_send"):
        _process_cluster(db, cluster, "key", "model", "now", 0.7, {}, "tok", summary)

    patch_call = next(c for c in captured if "media_inbox" in c["url"])
    assert patch_call["body"]["status"] == "done"
    assert "stage_reason" not in patch_call["body"]


# ── supplement_is_complete / biomarker_is_complete ────────────────────────────

def test_supplement_is_complete_with_id_and_dose():
    assert supplement_is_complete({
        "supplement_id": "uuid-123",
        "dose_amount": 500,
        "dose_unit": "mg",
    }) is True


def test_supplement_is_complete_missing_id():
    assert supplement_is_complete({"dose_amount": 500, "dose_unit": "mg"}) is False


def test_supplement_is_complete_missing_dose_amount():
    assert supplement_is_complete({"supplement_id": "uuid-123", "dose_unit": "mg"}) is False


def test_supplement_is_complete_missing_dose_unit():
    assert supplement_is_complete({"supplement_id": "uuid-123", "dose_amount": 500}) is False


def test_biomarker_is_complete_with_metric_value_unit():
    assert biomarker_is_complete({
        "metric_definition_id": "uuid-456",
        "value": 5.1,
        "unit": "mmol/L",
    }) is True


def test_biomarker_is_complete_missing_metric_id():
    assert biomarker_is_complete({"value": 5.1, "unit": "mmol/L"}) is False


def test_biomarker_is_complete_missing_value():
    assert biomarker_is_complete({"metric_definition_id": "uuid-456", "unit": "mmol/L"}) is False


def test_biomarker_is_complete_missing_unit():
    assert biomarker_is_complete({"metric_definition_id": "uuid-456", "value": 5.1}) is False


# ── supplement per-kind completeness gate ─────────────────────────────────────

_SUPP_FULL = {
    "name": "berberine",
    "supplement_id": "supp-uuid",
    "dose_amount": 500,
    "dose_unit": "mg",
    "taken_at": "2026-06-07T08:00:00Z",
    "confidence": 0.9,
}

_SUPP_NO_DOSE = {
    "name": "berberine",
    "supplement_id": "supp-uuid",
    "dose_amount": None,
    "dose_unit": None,
    "confidence": 0.9,
}

_SUPP_NO_MATCH = {
    "name": "unknown herb",
    "dose_amount": 500,
    "dose_unit": "mg",
    "confidence": 0.9,
}


def test_supplement_complete_with_match_autowrites():
    """Matched supplement with dose → p_force_stage=False (auto-write to prod)."""
    db, captured = _db_capture([
        (200, {"id": "sl-uuid", "status": "inserted"}),  # rpc
        (200, []),                                         # mark_rows
    ])
    from monitor.inbox_drain import _process_cluster
    summary = {"fetched": 0, "clustered": 0, "written": 0, "staged": 0, "failed": 0, "errors": []}
    cluster = [_item("r1", kind="supplement", caption="berberine 500mg")]

    with patch("monitor.inbox_drain.vision_extract", return_value=_SUPP_FULL), \
         patch("monitor.inbox_drain.lookup_supplement_by_name",
               return_value=[{"id": "supp-uuid", "name": "berberine"}]), \
         patch("monitor.inbox_drain.telegram_send"):
        _process_cluster(db, cluster, "key", "model", "now", 0.7, {}, "tok", summary)

    assert summary["written"] == 1
    assert summary["staged"] == 0
    rpc = next(c for c in captured if "maintainer_ingest_supplement" in c["url"])
    assert rpc["body"]["p_force_stage"] is False
    assert rpc["body"]["p_stage_reason"] is None


def test_supplement_missing_dose_stages():
    """Matched supplement but no dose → staged with 'incomplete: missing dose or unit'."""
    db, captured = _db_capture([
        (200, {"id": "stg-uuid", "status": "staged",
               "stage_reason": "incomplete: missing dose or unit"}),
        (200, []),
    ])
    from monitor.inbox_drain import _process_cluster
    summary = {"fetched": 0, "clustered": 0, "written": 0, "staged": 0, "failed": 0, "errors": []}
    cluster = [_item("r1", kind="supplement", caption="berberine")]

    with patch("monitor.inbox_drain.vision_extract", return_value=_SUPP_NO_DOSE), \
         patch("monitor.inbox_drain.lookup_supplement_by_name",
               return_value=[{"id": "supp-uuid", "name": "berberine"}]), \
         patch("monitor.inbox_drain.telegram_send"):
        _process_cluster(db, cluster, "key", "model", "now", 0.7, {}, "tok", summary)

    assert summary["staged"] == 1
    rpc = next(c for c in captured if "maintainer_ingest_supplement" in c["url"])
    assert rpc["body"]["p_force_stage"] is True
    assert rpc["body"]["p_stage_reason"] == "incomplete: missing dose or unit"


def test_supplement_no_match_stages():
    """Unresolved supplement (no lookup match) → staged with 'incomplete: no supplement match'."""
    db, captured = _db_capture([
        (200, {"id": "stg-uuid", "status": "staged",
               "stage_reason": "incomplete: no supplement match"}),
        (200, []),
    ])
    from monitor.inbox_drain import _process_cluster
    summary = {"fetched": 0, "clustered": 0, "written": 0, "staged": 0, "failed": 0, "errors": []}
    cluster = [_item("r1", kind="supplement", caption="unknown herb 500mg")]

    with patch("monitor.inbox_drain.vision_extract", return_value=_SUPP_NO_MATCH), \
         patch("monitor.inbox_drain.lookup_supplement_by_name", return_value=[]), \
         patch("monitor.inbox_drain.telegram_send"):
        _process_cluster(db, cluster, "key", "model", "now", 0.7, {}, "tok", summary)

    assert summary["staged"] == 1
    rpc = next(c for c in captured if "maintainer_ingest_supplement" in c["url"])
    assert rpc["body"]["p_force_stage"] is True
    assert rpc["body"]["p_stage_reason"] == "incomplete: no supplement match"


# ── write_supplement / write_biomarker now accept force_stage ─────────────────

def test_write_supplement_forwards_force_stage_true():
    """write_supplement(force_stage=True) sends p_force_stage=True to the RPC."""
    from monitor.inbox_drain import write_supplement

    db, captured = _db_capture([
        (200, {"id": "stg-uuid", "status": "staged"}),
        (200, []),
    ])
    rows = [_item("r1", kind="supplement")]
    write_supplement(db, rows, rows[0]["profile_id"],
                     {"name": "berberine"}, confidence=0.9, raw_text="berberine",
                     force_stage=True, stage_reason="incomplete: no supplement match")

    rpc = next(c for c in captured if "maintainer_ingest_supplement" in c["url"])
    assert rpc["body"]["p_force_stage"] is True
    assert rpc["body"]["p_stage_reason"] == "incomplete: no supplement match"


def test_write_supplement_forwards_force_stage_false():
    """write_supplement(force_stage=False) sends p_force_stage=False to the RPC."""
    from monitor.inbox_drain import write_supplement

    db, captured = _db_capture([
        (200, {"id": "sl-uuid", "status": "inserted"}),
        (200, []),
    ])
    rows = [_item("r1", kind="supplement")]
    write_supplement(db, rows, rows[0]["profile_id"],
                     {"supplement_id": "s1", "dose_amount": 500, "dose_unit": "mg"},
                     confidence=0.9, raw_text="berberine 500mg", force_stage=False)

    rpc = next(c for c in captured if "maintainer_ingest_supplement" in c["url"])
    assert rpc["body"]["p_force_stage"] is False


def test_write_biomarker_forwards_force_stage_true():
    """write_biomarker(force_stage=True) sends p_force_stage=True with stage_reason."""
    from monitor.inbox_drain import write_biomarker

    db, captured = _db_capture([
        (200, {"id": "stg-uuid", "status": "staged"}),
        (200, []),
    ])
    rows = [_item("r1", kind="lab")]
    write_biomarker(db, rows, rows[0]["profile_id"],
                    {"extracted_name": "glucose", "value": 95, "unit": "mg/dL"},
                    confidence=0.8, raw_text="glucose 95",
                    force_stage=True, stage_reason="incomplete: no metric match")

    rpc = next(c for c in captured if "maintainer_ingest_biomarker" in c["url"])
    assert rpc["body"]["p_force_stage"] is True
    assert rpc["body"]["p_stage_reason"] == "incomplete: no metric match"


# ── classification: unknown kind re-dispatched ───────────────────────────────

def test_unknown_kind_reclassified_as_food_list():
    """unknown kind + vision returns food list → food branch writes each item."""
    db, captured = _db_capture([
        (200, {"id": "f1", "status": "inserted"}),
        (200, {"id": "f2", "status": "inserted"}),
        (200, []),  # mark_rows once for the cluster
    ])
    from monitor.inbox_drain import _process_cluster
    summary = {"fetched": 0, "clustered": 0, "written": 0, "staged": 0, "failed": 0, "errors": []}
    cluster = [_item("r1", kind="unknown", caption="shake + 300g fish + 1 bowl rice")]

    classified = {
        "kind": "food",
        "data": [
            {"description": "protein shake", "calories": 320, "protein_g": 40, "confidence": 0.9},
            {"description": "grilled fish", "calories": 350, "protein_g": 60, "confidence": 0.9},
        ],
    }
    with patch("monitor.inbox_drain.vision_extract", return_value=classified), \
         patch("monitor.inbox_drain.telegram_send"):
        _process_cluster(db, cluster, "key", "model", "now", 0.7, {}, "tok", summary)

    rpc_calls = [c for c in captured if "maintainer_ingest_food" in c["url"]]
    assert len(rpc_calls) == 2
    assert summary["written"] == 1
    assert summary["failed"] == 0


def test_unknown_kind_reclassified_as_supplement():
    """unknown kind + vision returns supplement → supplement branch runs completeness gate."""
    db, captured = _db_capture([
        (200, {"id": "sl-uuid", "status": "inserted"}),  # supplement rpc
        (200, []),                                         # mark_rows
    ])
    from monitor.inbox_drain import _process_cluster
    summary = {"fetched": 0, "clustered": 0, "written": 0, "staged": 0, "failed": 0, "errors": []}
    cluster = [_item("r1", kind="unknown", caption="berberine + omega-3")]

    classified = {
        "kind": "supplement",
        "data": {
            "name": "berberine",
            "dose_amount": 500,
            "dose_unit": "mg",
            "taken_at": "2026-06-07T08:00:00Z",
            "confidence": 0.9,
        },
    }
    with patch("monitor.inbox_drain.vision_extract", return_value=classified), \
         patch("monitor.inbox_drain.lookup_supplement_by_name",
               return_value=[{"id": "supp-uuid", "name": "berberine"}]), \
         patch("monitor.inbox_drain.telegram_send"):
        _process_cluster(db, cluster, "key", "model", "now", 0.7, {}, "tok", summary)

    rpc_calls = [c for c in captured if "maintainer_ingest_supplement" in c["url"]]
    assert len(rpc_calls) == 1
    assert rpc_calls[0]["body"]["p_force_stage"] is False
    assert summary["written"] == 1


def test_unknown_kind_stays_unknown_stages_with_could_not_classify():
    """unknown kind + vision also returns unknown → staged with 'could not classify'."""
    db, captured = _db_capture([
        (200, {"id": "stg-uuid", "status": "staged",
               "stage_reason": "could not classify"}),
        (200, []),
    ])
    from monitor.inbox_drain import _process_cluster
    summary = {"fetched": 0, "clustered": 0, "written": 0, "staged": 0, "failed": 0, "errors": []}
    cluster = [_item("r1", kind="unknown", caption="???")]

    with patch("monitor.inbox_drain.vision_extract",
               return_value={"kind": "unknown", "data": {}}), \
         patch("monitor.inbox_drain.telegram_send"):
        _process_cluster(db, cluster, "key", "model", "now", 0.7, {}, "tok", summary)

    assert summary["staged"] == 1
    rpc = next(c for c in captured if "maintainer_ingest_food" in c["url"])
    assert rpc["body"]["p_stage_reason"] == "could not classify"
