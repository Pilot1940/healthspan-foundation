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
        # push_log is the cross-run brief-dedup ledger — always empty in unit tests. Short-circuit
        # it (GET dedup check → [], POST record → ignored) WITHOUT consuming the response cycle, so
        # adding the dedup didn't shift every downstream mock response.
        if "push_log" in str(request.url):
            return httpx.Response(200, content=b"[]", headers={"content-type": "application/json"})
        # Same treatment for the clarify auto-match + orphan-sweep staged-row lookups
        # (media_inbox?status=eq.staged GETs) — always empty here; both layers have their
        # own dedicated unit tests with a MagicMock db.
        if request.method == "GET" and "status=eq.staged" in str(request.url):
            return httpx.Response(200, content=b"[]", headers={"content-type": "application/json"})
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
    # vision_extract retries once on a parse miss, then surfaces the unparseable-JSON reason
    # (with the model's stop_reason) so the caller STAGES rather than fails.
    assert result.get("_stage_reason") == "vision returned unparseable JSON (stop_reason=None)"
    assert "_error" not in result


def test_vision_extract_empty_body_returns_stage_reason():
    """Empty/shapeless response body → _stage_reason (fail-safe), never a crash."""
    empty_response = {}  # no "content" key
    with patch("monitor.inbox_drain.httpx.post", return_value=_mock_http_resp(empty_response)):
        result = vision_extract("key", "model", [], "", "food", "now")

    assert result["confidence"] == 0.0
    # Missing "content" key → KeyError caught as a bad response shape (retried once, then staged).
    assert result.get("_stage_reason") == "vision returned unexpected response shape: 'content'"


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


def test_supplement_rpc_args_missing_supplement_id_passes_none():
    """When lookup_supplement_by_name finds no match, extracted has no supplement_id.
    Building the RPC args must not KeyError — it passes p_supplement_id=None and lets the
    RPC decide (the supplement write path is inline in _process_cluster now; there is no
    write_supplement wrapper). Preserves the original None-safety invariant."""
    from monitor.inbox_drain import _supplement_rpc_args

    args = _supplement_rpc_args(
        "profile-1",
        {"name": "Unknown Herb", "dose_amount": 500, "dose_unit": "mg"},
        0.6, "Unknown Herb 500mg", False,
    )
    assert args["p_supplement_id"] is None  # absent → None, never a KeyError
    assert args["p_dose_amount"] == 500
    assert args["p_extracted_name"] == "Unknown Herb"


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
         patch("monitor.inbox_drain.lookup_food_reference", return_value=None), \
         patch("monitor.inbox_drain.lookup_viome_verdicts", return_value=[]), \
         patch("monitor.inbox_drain.telegram_send", return_value=4242), \
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
         patch("monitor.inbox_drain.lookup_food_reference", return_value=None), \
         patch("monitor.inbox_drain.lookup_viome_verdicts", return_value=[]), \
         patch("monitor.inbox_drain.telegram_send", return_value=4242):
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
         patch("monitor.inbox_drain.telegram_send", return_value=4242):
        summary = run_once(db, {
            "push.inbox_settle_sec": "0",
            "ingest.confidence_threshold": "0.7",
            "drain.vision_model": '"claude-sonnet-4-6"',
        }, "api-key", "tg-token")

    assert summary["failed"] == 1
    assert len(summary["errors"]) == 1


def test_run_once_photo_never_briefs():
    """C3 regression: a caption-less / ambiguous PHOTO that the model classifies as
    'brief' must STAGE for clarification (an image is ALWAYS a log), never silently
    compose a brief and drop the photo. Reproduces the 2026-06-09 French-press miss."""
    identities = [{"chat_id": 99, "is_minor": False,
                   "profile_id": _PC_PROFILE_ID, "display_name": "PC"}]
    item = _item("r1", kind="unknown", caption="")
    item["storage_path"] = "telegram/x/1.jpg"  # a photo IS attached
    db = _db([
        (200, identities),                       # telegram_identities
        (200, [item]),                           # fetch_settled
        (200, [{"id": "r1", "status": "processing"}]),  # claim r1
        (200, []),                               # mark_rows staged PATCH
    ])

    with patch("monitor.inbox_drain.vision_extract", return_value={"kind": "brief"}), \
         patch("monitor.inbox_drain.get_signed_url", return_value="https://signed/url"), \
         patch("monitor.inbox_drain._download_image", return_value=(b"img", "image/jpeg")), \
         patch("monitor.inbox_drain.describe_stage", return_value="what is this?"), \
         patch("monitor.inbox_drain.telegram_send", return_value=None):
        summary = run_once(db, {
            "push.inbox_settle_sec": "0",
            "ingest.confidence_threshold": "0.7",
            "drain.vision_model": '"claude-sonnet-4-6"',
        }, "api-key", "tg-token")

    # Image present + model said "brief" → staged for clarification, nothing written,
    # and (crucially) NOT queued as a brief.
    assert summary["staged"] == 1
    assert summary["written"] == 0


def test_run_once_textonly_brief_still_briefs():
    """Guard is image-gated: a text-only 'brief' classification (no photo) is still a
    brief — added to brief_profiles, nothing staged."""
    identities = [{"chat_id": 99, "is_minor": False,
                   "profile_id": _PC_PROFILE_ID, "display_name": "PC"}]
    item = _item("r1", kind="unknown", caption="how am I doing?")  # storage_path stays None
    db = _db([
        (200, identities),
        (200, [item]),
        (200, [{"id": "r1", "status": "processing"}]),
        (200, []),  # mark_rows done PATCH
    ])

    with patch("monitor.inbox_drain.vision_extract", return_value={"kind": "brief"}), \
         patch("monitor.brief.compose_brief", return_value="brief text") as mock_brief, \
         patch("monitor.inbox_drain.telegram_send", return_value=None):
        summary = run_once(db, {
            "push.inbox_settle_sec": "0",
            "ingest.confidence_threshold": "0.7",
            "drain.vision_model": '"claude-sonnet-4-6"',
        }, "api-key", "tg-token")

    assert summary["staged"] == 0
    assert summary["written"] == 0
    mock_brief.assert_called_once()  # text-only brief still composes a brief


_DEA_PROFILE_ID = "3eed5503-a26f-4b88-bb76-075208fa5de3"


def test_run_once_explicit_brief_bypasses_dedup():
    """An EXPLICITLY-requested brief ('how am I doing?') must send even when a post-log brief
    went out within brief.dedup_sec. The dedup is only for burst post-log nudges. Regression
    for 2026-06-09: a user logged food (auto brief at 7:17), asked for a brief at 7:22, got
    'On it…' but no brief — the 4½-min-old auto brief deduped the explicit request away."""
    identities = [{"chat_id": 99, "is_minor": False,
                   "profile_id": _PC_PROFILE_ID, "display_name": "PC"}]
    item = _item("r1", kind="unknown", caption="how am I doing?")  # text-only, no food write

    responses = [
        (200, identities),
        (200, [item]),
        (200, [{"id": "r1", "status": "processing"}]),
        (200, []),  # mark_rows done
    ]
    idx = 0

    def _handler(request: httpx.Request) -> httpx.Response:
        nonlocal idx
        if "push_log" in str(request.url):
            # GET = dedup check → a brief sent "just now" (would skip an AUTO brief). POST = insert.
            body = [{"sent_at": "2999-01-01T00:00:00Z"}] if request.method == "GET" else []
            return httpx.Response(200, content=json.dumps(body).encode(),
                                  headers={"content-type": "application/json"})
        status, b = responses[idx % len(responses)]
        idx += 1
        return httpx.Response(status, content=json.dumps(b).encode(),
                              headers={"content-type": "application/json"})

    client = httpx.Client(transport=httpx.MockTransport(_handler))
    db = DbRest.__new__(DbRest)
    db._base = "https://test.supabase.co"
    db._anon_key = "anon"
    db._jwt = "jwt"
    db._client = client

    with patch("monitor.inbox_drain.vision_extract", return_value={"kind": "brief"}), \
         patch("monitor.brief.compose_brief", return_value="brief text") as mock_brief, \
         patch("monitor.inbox_drain.telegram_send", return_value=None):
        run_once(db, {
            "push.inbox_settle_sec": "0",
            "ingest.confidence_threshold": "0.7",
            "drain.vision_model": '"claude-sonnet-4-6"',
        }, "api-key", "tg-token")

    mock_brief.assert_called_once()  # explicit request sends despite a recent brief


def test_run_once_minor_no_brief_without_consent():
    """A MINOR's brief request must NOT compose a brief unless their profile_id is in the
    `brief.minor_optin_profile_ids` consent allowlist (default: minors get confirmations only)."""
    identities = [{"chat_id": 99, "is_minor": True,
                   "profile_id": _DEA_PROFILE_ID, "display_name": "Dea"}]
    item = _item("r1", kind="unknown", caption="how am I doing?")  # text-only
    item["profile_id"] = _DEA_PROFILE_ID
    db = _db([
        (200, identities),
        (200, [item]),
        (200, [{"id": "r1", "status": "processing"}]),
        (200, []),
    ])

    with patch("monitor.inbox_drain.vision_extract", return_value={"kind": "brief"}), \
         patch("monitor.brief.compose_brief", return_value="brief text") as mock_brief, \
         patch("monitor.inbox_drain.telegram_send", return_value=None):
        run_once(db, {
            "push.inbox_settle_sec": "0",
            "ingest.confidence_threshold": "0.7",
            "drain.vision_model": '"claude-sonnet-4-6"',
            # no brief.minor_optin_profile_ids → Dea is not consented
        }, "api-key", "tg-token")

    mock_brief.assert_not_called()  # minor without consent → no brief


def test_run_once_minor_brief_with_consent():
    """A MINOR in the consent allowlist DOES receive a (minor-safe) brief — PC-consented Dea."""
    identities = [{"chat_id": 99, "is_minor": True,
                   "profile_id": _DEA_PROFILE_ID, "display_name": "Dea"}]
    item = _item("r1", kind="unknown", caption="how am I doing?")  # text-only
    item["profile_id"] = _DEA_PROFILE_ID
    db = _db([
        (200, identities),
        (200, [item]),
        (200, [{"id": "r1", "status": "processing"}]),
        (200, []),
    ])

    with patch("monitor.inbox_drain.vision_extract", return_value={"kind": "brief"}), \
         patch("monitor.brief.compose_brief", return_value="brief text") as mock_brief, \
         patch("monitor.inbox_drain.telegram_send", return_value=None):
        run_once(db, {
            "push.inbox_settle_sec": "0",
            "ingest.confidence_threshold": "0.7",
            "drain.vision_model": '"claude-sonnet-4-6"',
            "brief.minor_optin_profile_ids": [_DEA_PROFILE_ID],
        }, "api-key", "tg-token")

    mock_brief.assert_called_once()  # consented minor → brief composes


def test_run_once_logged_food_stores_supersede_link():
    """Inserted food stores clarify_message_id (the 'Logged' msg id) + logged_food_ids so a
    REPLY to that message can SUPERSEDE the entry (webhook deletes those food_logs, re-queues)
    instead of double-counting."""
    db = _make_db_for_run_once([
        (200, {"id": "food-uuid-1", "status": "inserted"}),  # maintainer_ingest_food
        (200, []),
    ])
    captured: list[tuple] = []
    orig_update = db.update
    def spy(table, match, patch, **kwargs):
        captured.append((table, patch))
        return orig_update(table, match, patch, **kwargs)
    db.update = spy  # type: ignore[method-assign]

    with patch("monitor.inbox_drain.vision_extract", return_value=_FOOD_EXTRACTION), \
         patch("monitor.inbox_drain.lookup_food_reference", return_value=None), \
         patch("monitor.inbox_drain.lookup_viome_verdicts", return_value=[]), \
         patch("monitor.inbox_drain.fetch_today_food_totals", return_value={}), \
         patch("monitor.inbox_drain.fetch_today_supplement_counts", return_value={}), \
         patch("monitor.inbox_drain.telegram_send", return_value=7777):
        run_once(db, {
            "push.inbox_settle_sec": "0",
            "ingest.confidence_threshold": "0.7",
            "drain.vision_model": '"claude-sonnet-4-6"',
        }, "api-key", "tg-token")

    store = [p for (t, p) in captured if t == "media_inbox" and "clarify_message_id" in p]
    assert store, "expected a media_inbox update storing clarify_message_id for logged food"
    assert store[0]["clarify_message_id"] == 7777
    assert store[0].get("logged_food_ids") == ["food-uuid-1"]


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
    """The 0.5-confidence full-macro shake must be WRITTEN (force_stage=False) at the
    PRODUCTION confidence threshold of 0.3 (ingest.confidence_threshold in system_config).
    The enforced gate force-stages only items BELOW that threshold; 0.5 >= 0.3 so a
    complete shake auto-logs — low SELF-REPORTED confidence alone must not block a complete
    extraction (the original 0.7 threshold here was a test artefact, not the prod value)."""
    db, captured = _db_capture([
        (200, {"id": "food-uuid", "status": "inserted"}),  # rpc
        (200, []),                                          # mark_rows
    ])
    from monitor.inbox_drain import _process_cluster
    summary = {"fetched": 0, "clustered": 0, "written": 0, "staged": 0, "failed": 0, "errors": []}
    cluster = [_item("r1", caption="protein shake")]

    with patch("monitor.inbox_drain.vision_extract", return_value=_COMPLETE_SHAKE), \
         patch("monitor.inbox_drain.lookup_food_reference", return_value=None), \
         patch("monitor.inbox_drain.lookup_viome_verdicts", return_value=[]), \
         patch("monitor.inbox_drain.telegram_send", return_value=4242):
        _process_cluster(db, cluster, "key", "model", "now", 0.3, {}, "tok", summary)

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
         patch("monitor.inbox_drain.lookup_food_reference", return_value=None), \
         patch("monitor.inbox_drain.lookup_viome_verdicts", return_value=[]), \
         patch("monitor.inbox_drain.telegram_send", return_value=4242):
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
         patch("monitor.inbox_drain.lookup_food_reference", return_value=None), \
         patch("monitor.inbox_drain.lookup_viome_verdicts", return_value=[]), \
         patch("monitor.inbox_drain.telegram_send", return_value=4242):
        _process_cluster(db, cluster, "key", "model", "now", 0.7, {}, "tok", summary)

    rpc_calls = [c for c in captured if "maintainer_ingest_food" in c["url"]]
    assert len(rpc_calls) == 3
    assert summary["written"] == 3  # written counts per ITEM; all 3 items inserted
    assert summary["failed"] == 0
    # Cluster STATUS is marked exactly once (by aggregate status), not per-item.
    status_patches = [c for c in captured
                      if "media_inbox" in c["url"] and "status" in (c["body"] or {})]
    assert len(status_patches) == 1
    assert status_patches[0]["body"]["status"] == "done"
    # A separate media_inbox PATCH stores the supersede-on-reply link (clarify_message_id +
    # logged_food_ids) — not a status mark; it carries the inserted food_logs ids.
    link_patches = [c for c in captured
                    if "media_inbox" in c["url"] and "logged_food_ids" in (c["body"] or {})]
    assert len(link_patches) == 1
    assert link_patches[0]["body"]["logged_food_ids"] == ["f1", "f2", "f3"]


def test_food_list_viome_flag_is_per_item_not_meal_wide():
    """A flagged ingredient (mushrooms=superfood) must tag ONLY the dish that contains
    it — never smear across every row in the photo cluster (the 'mushrooms x5' bug)."""
    db, captured = _db_capture([
        (200, {"id": "f1", "status": "inserted"}),  # fettuccine (has mushrooms)
        (200, {"id": "f2", "status": "inserted"}),  # salad (no mushrooms)
        (200, []),
    ])
    from monitor.inbox_drain import _process_cluster
    summary = {"fetched": 0, "clustered": 0, "written": 0, "staged": 0, "failed": 0, "errors": []}
    cluster = [_item("r1", caption="fettuccine and a salad")]

    food_list = [
        {"description": "fettuccine with mushrooms", "calories": 420, "protein_g": 14,
         "foods": [{"name": "mushrooms"}], "confidence": 0.9},
        {"description": "garden salad", "calories": 90, "protein_g": 3,
         "foods": [{"name": "lettuce"}], "confidence": 0.9},
    ]
    viome = [{"item": "mushrooms", "effective_classification": "superfood", "reason": ""}]

    with patch("monitor.inbox_drain.vision_extract", return_value=food_list), \
         patch("monitor.inbox_drain.lookup_food_reference", return_value=None), \
         patch("monitor.inbox_drain.lookup_past_food_macros", return_value=None), \
         patch("monitor.inbox_drain.lookup_viome_verdicts", return_value=viome), \
         patch("monitor.inbox_drain.telegram_send", return_value=4242):
        _process_cluster(db, cluster, "key", "model", "now", 0.7, {}, "tok", summary)

    # Per-row verdict PATCHes on food_logs — one per inserted item, scoped by id.
    verdict_patches = {
        c["url"].split("id=eq.")[1].split("&")[0]: c["body"]
        for c in captured
        if "food_logs" in c["url"] and "flags" in (c["body"] or {})
    }
    assert verdict_patches["f1"] == {"verdict": "superfood", "flags": ["mushrooms"]}
    assert verdict_patches["f2"] == {"verdict": "clean", "flags": []}


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
         patch("monitor.inbox_drain.lookup_food_reference", return_value=None), \
         patch("monitor.inbox_drain.lookup_viome_verdicts", return_value=[]), \
         patch("monitor.inbox_drain.telegram_send", return_value=4242):
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
         patch("monitor.inbox_drain.telegram_send", return_value=4242):
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
         patch("monitor.inbox_drain.telegram_send", return_value=4242):
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
         patch("monitor.inbox_drain.lookup_food_reference", return_value=None), \
         patch("monitor.inbox_drain.lookup_viome_verdicts", return_value=[]), \
         patch("monitor.inbox_drain.fetch_today_food_totals", return_value={}), \
         patch("monitor.inbox_drain.fetch_today_supplement_counts", return_value={}), \
         patch("monitor.inbox_drain.telegram_send", return_value=4242) as mock_tg:
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
         patch("monitor.inbox_drain.lookup_food_reference", return_value=None), \
         patch("monitor.inbox_drain.lookup_viome_verdicts", return_value=[]), \
         patch("monitor.inbox_drain.telegram_send", return_value=4242) as mock_tg:
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
         patch("monitor.inbox_drain.telegram_send", return_value=4242) as mock_tg:
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
         patch("monitor.inbox_drain.lookup_food_reference", return_value=None), \
         patch("monitor.inbox_drain.lookup_viome_verdicts", return_value=[]), \
         patch("monitor.inbox_drain.fetch_today_food_totals", return_value=totals), \
         patch("monitor.inbox_drain.fetch_today_supplement_counts", return_value={"taken": 4, "total": 16}), \
         patch("monitor.inbox_drain.telegram_send", return_value=4242) as mock_tg:
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
         patch("monitor.inbox_drain.lookup_food_reference", return_value=None), \
         patch("monitor.inbox_drain.lookup_viome_verdicts", return_value=[]), \
         patch("monitor.inbox_drain.fetch_today_food_totals") as mock_totals, \
         patch("monitor.inbox_drain.telegram_send", return_value=4242) as mock_tg:
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
         patch("monitor.inbox_drain.telegram_send", return_value=4242):
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
         patch("monitor.inbox_drain.telegram_send", return_value=4242):
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
         patch("monitor.inbox_drain.lookup_food_reference", return_value=None), \
         patch("monitor.inbox_drain.lookup_viome_verdicts", return_value=[]), \
         patch("monitor.inbox_drain.telegram_send", return_value=4242):
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
         patch("monitor.inbox_drain.lookup_food_reference", return_value=None), \
         patch("monitor.inbox_drain.lookup_viome_verdicts", return_value=[]), \
         patch("monitor.inbox_drain.telegram_send", return_value=4242):
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
         patch("monitor.inbox_drain.telegram_send", return_value=4242):
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
         patch("monitor.inbox_drain.lookup_food_reference", return_value=None), \
         patch("monitor.inbox_drain.lookup_viome_verdicts", return_value=[]), \
         patch("monitor.inbox_drain.telegram_send", return_value=4242):
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
    # Dose is now OPTIONAL — a resolved supplement id alone is complete (dose defaults
    # from the regimen, or "I took it" is a valid adherence record without a dose).
    assert supplement_is_complete({"supplement_id": "uuid-123", "dose_unit": "mg"}) is True


def test_supplement_is_complete_missing_dose_unit():
    # Dose is now OPTIONAL — id-only is complete (see above).
    assert supplement_is_complete({"supplement_id": "uuid-123", "dose_amount": 500}) is True


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
         patch("monitor.inbox_drain.telegram_send", return_value=4242):
        _process_cluster(db, cluster, "key", "model", "now", 0.7, {}, "tok", summary)

    assert summary["written"] == 1
    assert summary["staged"] == 0
    rpc = next(c for c in captured if "maintainer_ingest_supplement" in c["url"])
    assert rpc["body"]["p_force_stage"] is False
    assert rpc["body"]["p_stage_reason"] is None


def test_supplement_missing_dose_autowrites():
    """Matched supplement with NO dose now AUTO-WRITES (dose is optional). With no regimen
    dose to default from, it logs id-only as a valid adherence record — force_stage=False.
    (Was: missing dose staged. The completeness contract changed to id-only.)"""
    db, captured = _db_capture([
        (200, {"id": "sl-uuid", "status": "inserted"}),
        (200, []),
    ])
    from monitor.inbox_drain import _process_cluster
    summary = {"fetched": 0, "clustered": 0, "written": 0, "staged": 0, "failed": 0, "errors": []}
    cluster = [_item("r1", kind="supplement", caption="berberine")]

    with patch("monitor.inbox_drain.vision_extract", return_value=_SUPP_NO_DOSE), \
         patch("monitor.inbox_drain.lookup_supplement_by_name",
               return_value=[{"id": "supp-uuid", "name": "berberine"}]), \
         patch("monitor.inbox_drain.lookup_regimen_dose", return_value=None), \
         patch("monitor.inbox_drain.telegram_send", return_value=4242):
        _process_cluster(db, cluster, "key", "model", "now", 0.7, {}, "tok", summary)

    assert summary["written"] == 1
    assert summary["staged"] == 0
    rpc = next(c for c in captured if "maintainer_ingest_supplement" in c["url"])
    assert rpc["body"]["p_force_stage"] is False
    assert rpc["body"]["p_stage_reason"] is None


def test_supplement_no_match_stages():
    """Unresolved supplement (no lookup match, even after learn-on-clarify) → staged with
    'incomplete: no supplement match'. This invariant is STILL live: a supplement that can't
    be resolved to an id must clarify, never auto-write. A SPECIFIC name triggers a
    learn_supplement attempt first; here the re-lookup still misses (mock stays empty)."""
    db, captured = _db_capture([
        (200, {"learned": False}),                       # learn_supplement RPC (no-op here)
        (200, {"id": "stg-uuid", "status": "staged",
               "stage_reason": "incomplete: no supplement match"}),
        (200, []),                                        # mark_rows
    ])
    from monitor.inbox_drain import _process_cluster
    summary = {"fetched": 0, "clustered": 0, "written": 0, "staged": 0, "failed": 0, "errors": []}
    cluster = [_item("r1", kind="supplement", caption="unknown herb 500mg")]

    with patch("monitor.inbox_drain.vision_extract", return_value=_SUPP_NO_MATCH), \
         patch("monitor.inbox_drain.lookup_supplement_by_name", return_value=[]), \
         patch("monitor.inbox_drain.describe_stage", return_value="which supplement?"), \
         patch("monitor.inbox_drain.telegram_send", return_value=4242):
        _process_cluster(db, cluster, "key", "model", "now", 0.7, {}, "tok", summary)

    assert summary["staged"] == 1
    rpc = next(c for c in captured if "maintainer_ingest_supplement" in c["url"])
    assert rpc["body"]["p_force_stage"] is True
    assert rpc["body"]["p_stage_reason"] == "incomplete: no supplement match"


# ── supplement RPC args forward force_stage (inline write path) ───────────────
# The write_supplement wrapper was removed — supplements write inline in
# _process_cluster's multi-item loop via maintainer_ingest_supplement, with args built
# by _supplement_rpc_args. These assert the force_stage/stage_reason are forwarded
# through that arg builder (the surface the wrapper used to provide).

def test_supplement_rpc_args_forwards_force_stage_true():
    """_supplement_rpc_args(force_stage=True) sends p_force_stage=True + stage_reason."""
    from monitor.inbox_drain import _supplement_rpc_args

    args = _supplement_rpc_args(
        "profile-1", {"name": "berberine"}, 0.9, "berberine",
        True, "incomplete: no supplement match",
    )
    assert args["p_force_stage"] is True
    assert args["p_stage_reason"] == "incomplete: no supplement match"


def test_supplement_rpc_args_forwards_force_stage_false():
    """_supplement_rpc_args(force_stage=False) sends p_force_stage=False."""
    from monitor.inbox_drain import _supplement_rpc_args

    args = _supplement_rpc_args(
        "profile-1",
        {"supplement_id": "s1", "dose_amount": 500, "dose_unit": "mg"},
        0.9, "berberine 500mg", False,
    )
    assert args["p_force_stage"] is False


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
         patch("monitor.inbox_drain.lookup_food_reference", return_value=None), \
         patch("monitor.inbox_drain.lookup_viome_verdicts", return_value=[]), \
         patch("monitor.inbox_drain.telegram_send", return_value=4242):
        _process_cluster(db, cluster, "key", "model", "now", 0.7, {}, "tok", summary)

    rpc_calls = [c for c in captured if "maintainer_ingest_food" in c["url"]]
    assert len(rpc_calls) == 2
    assert summary["written"] == 2  # written counts per ITEM; both items inserted
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
         patch("monitor.inbox_drain.telegram_send", return_value=4242):
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
         patch("monitor.inbox_drain.telegram_send", return_value=4242):
        _process_cluster(db, cluster, "key", "model", "now", 0.7, {}, "tok", summary)

    assert summary["staged"] == 1
    rpc = next(c for c in captured if "maintainer_ingest_food" in c["url"])
    assert rpc["body"]["p_stage_reason"] == "could not classify"


# ── 040 write-contract audit ──────────────────────────────────────────────────

def test_food_vision_prompt_has_no_unknown_meal_type():
    """The (single) vision prompt's food schema must not offer 'unknown' as a meal_type
    option. 'unknown' is not in food_logs_meal_type_check; offering it causes CHECK
    violations. The regex-gate removal collapsed _VISION_PROMPTS to one 'unknown' key
    (the router+extractor) — the food meal_type example now lives there.
    """
    from monitor.inbox_drain import _VISION_PROMPTS
    prompt = _VISION_PROMPTS["unknown"]
    # 'unknown' must not appear as a meal_type value (it could still appear in prose)
    assert "meal_type" in prompt, "vision prompt should still instruct on meal_type"
    # The prompt uses 'breakfast|lunch|dinner|...' format — check 'unknown' not in that list
    import re as _re
    m = _re.search(r'"meal_type":"([^"]+)"', prompt)
    assert m, "vision prompt should have meal_type example"
    meal_type_options = m.group(1).split("|")
    assert "unknown" not in meal_type_options, (
        f"'unknown' must not be a meal_type option — it fails food_logs_meal_type_check. "
        f"Got: {meal_type_options}"
    )


def test_food_vision_prompt_meal_types_all_valid():
    """All meal_type options in the (single) vision prompt must be in
    food_logs_meal_type_check."""
    from monitor.inbox_drain import _VISION_PROMPTS
    import re as _re
    valid = {"breakfast", "lunch", "dinner", "snack", "drink", "supplement"}
    m = _re.search(r'"meal_type":"([^"]+)"', _VISION_PROMPTS["unknown"])
    assert m
    for opt in m.group(1).split("|"):
        assert opt in valid, (
            f"meal_type '{opt}' not in food_logs_meal_type_check valid set {valid}"
        )


def test_supplement_rpc_args_source_is_telegram():
    """_supplement_rpc_args always sends source='telegram' — the drain source."""
    from monitor.inbox_drain import _supplement_rpc_args
    args = _supplement_rpc_args(
        "profile-1",
        {"supplement_id": "supp-uuid", "dose_amount": 500, "dose_unit": "mg"},
        0.9, "berberine 500mg", False,
    )
    assert args["p_source"] == "telegram", (
        "supplement RPC source must be 'telegram' — the drain source; "
        "supplement_intake_logs.source CHECK must include 'telegram' (migration 040)"
    )


def test_biomarker_rpc_args_source_is_telegram():
    """_biomarker_rpc_args always sends source='telegram'."""
    from monitor.inbox_drain import _biomarker_rpc_args
    args = _biomarker_rpc_args(
        "profile-1",
        {"metric_definition_id": "metric-uuid", "value": 98.0, "unit": "mg/dL"},
        0.9, "glucose 98", False,
    )
    assert args["p_source"] == "telegram"


def test_food_rpc_args_source_is_telegram():
    """_food_rpc_args always sends source='telegram'."""
    from monitor.inbox_drain import _food_rpc_args
    args = _food_rpc_args(
        "profile-1",
        {"meal_type": "lunch", "description": "chicken", "calories": 400},
        0.9, "chicken 400 kcal", False,
    )
    assert args["p_source"] == "telegram"


# ── 041 food_reference + viome cross-check ────────────────────────────────────

_HOORAY_VISION = {
    "meal_type": "drink",
    "description": "strawberry protein shake",
    "calories": 180,   # vision estimate — will be overridden by reference
    "protein_g": 20.0,
    "carbs_g": 15.0,
    "fat_g": 3.0,
    "fiber_g": 1.0,
    "foods": [{"name": "strawberry shake", "amount": 340, "unit": "ml", "calories": 180}],
    "logged_at": "2026-06-07T08:00:00Z",
    "confidence": 0.7,
}

_HOORAY_REF_ROW = {
    "id": "ref-uuid-hooray",
    "name": "Hooray Strawberry Shake",
    "calories": 250,
    "protein_g": 31.0,
    "carbs_g": 22.0,
    "fat_g": 4.0,
    "fiber_g": 0.0,
    "serving_desc": "340ml shake",
    "brand": "Hooray",
    "verified": True,
    "is_global": True,
}


def test_hooray_resolves_global_macros():
    """When food_reference returns Hooray, vision macros are replaced with reference macros
    before calling maintainer_ingest_food. Verifies caption 'Hooray' triggers the match."""
    db, captured = _db_capture([
        (200, {"id": "food-uuid", "status": "inserted"}),  # maintainer_ingest_food
        (200, []),                                          # mark_rows
    ])
    from monitor.inbox_drain import _process_cluster
    summary = {"fetched": 0, "clustered": 0, "written": 0, "staged": 0, "failed": 0, "errors": []}
    cluster = [_item("r1", caption="Hooray")]

    with patch("monitor.inbox_drain.vision_extract", return_value=_HOORAY_VISION), \
         patch("monitor.inbox_drain.lookup_food_reference", return_value=_HOORAY_REF_ROW) as mock_ref, \
         patch("monitor.inbox_drain.lookup_viome_verdicts", return_value=[]), \
         patch("monitor.inbox_drain.telegram_send", return_value=4242):
        _process_cluster(db, cluster, "key", "model", "now", 0.7, {}, "tok", summary)

    # lookup_food_reference was called with candidates that include the caption "Hooray"
    call_candidates = mock_ref.call_args[0][1]  # second positional arg is candidates list
    assert "Hooray" in call_candidates, f"caption 'Hooray' must be in candidates; got: {call_candidates}"

    # The RPC was called with reference macros, NOT vision estimates
    rpc_call = next(c for c in captured if "maintainer_ingest_food" in c["url"])
    assert rpc_call["body"]["p_calories"] == 250, "calories must come from reference, not vision"
    assert rpc_call["body"]["p_protein_g"] == 31.0
    assert rpc_call["body"]["p_carbs_g"] == 22.0
    assert rpc_call["body"]["p_fat_g"] == 4.0
    assert summary["written"] == 1


def test_viome_avoid_food_flags_in_confirmation():
    """When viome_verdict returns 'avoid', the Telegram message includes a warning.
    Scope isolation (PC's coconut-milk verdict not appearing for Dea) is enforced by SQL;
    this test verifies the correct profile_id is forwarded to the RPC and warning is shown."""
    db, captured = _db_capture([
        (200, {"id": "food-uuid", "status": "inserted"}),
        (200, []),
    ])
    from monitor.inbox_drain import _process_cluster
    summary = {"fetched": 0, "clustered": 0, "written": 0, "staged": 0, "failed": 0, "errors": []}

    coconut_dish = {**_FOOD_EXTRACTION, "description": "Thai green curry",
                    "foods": [{"name": "coconut milk", "amount": 100, "unit": "ml", "calories": 180}]}
    avoid_verdict = [{"item": "coconut milk", "effective_classification": "avoid",
                      "reason": "Viome: inflammatory for your gut profile"}]

    with patch("monitor.inbox_drain.vision_extract", return_value=coconut_dish), \
         patch("monitor.inbox_drain.lookup_food_reference", return_value=None), \
         patch("monitor.inbox_drain.lookup_viome_verdicts", return_value=avoid_verdict) as mock_viome, \
         patch("monitor.inbox_drain.telegram_send", return_value=4242) as mock_tg:
        _process_cluster(db, cluster := [_item("r1", caption="thai curry")],
                         "key", "model", "now", 0.7, {}, "tok", summary)

    # Correct profile_id forwarded
    viome_call_profile = mock_viome.call_args[0][2]
    assert viome_call_profile == _PC_PROFILE_ID

    msg = mock_tg.call_args.args[2]
    assert "⚠️" in msg, f"avoid warning missing from: {msg}"
    assert "coconut milk" in msg.lower(), f"ingredient name missing from: {msg}"
    assert "AVOID" in msg, f"AVOID classification missing from: {msg}"


def test_viome_superfood_flags_positive_in_confirmation():
    """superfood verdict → positive ✅ line in Telegram confirmation (not a warning)."""
    db, captured = _db_capture([
        (200, {"id": "food-uuid", "status": "inserted"}),
        (200, []),
    ])
    from monitor.inbox_drain import _process_cluster
    summary = {"fetched": 0, "clustered": 0, "written": 0, "staged": 0, "failed": 0, "errors": []}

    blueberry_dish = {**_FOOD_EXTRACTION, "description": "blueberry bowl",
                      "foods": [{"name": "blueberry", "amount": 100, "unit": "g", "calories": 57}]}
    superfood_verdict = [{"item": "blueberry", "effective_classification": "superfood",
                          "reason": "antioxidant — top 10 for your profile"}]

    with patch("monitor.inbox_drain.vision_extract", return_value=blueberry_dish), \
         patch("monitor.inbox_drain.lookup_food_reference", return_value=None), \
         patch("monitor.inbox_drain.lookup_viome_verdicts", return_value=superfood_verdict), \
         patch("monitor.inbox_drain.telegram_send", return_value=4242) as mock_tg:
        _process_cluster(db, [_item("r1", caption="blueberry bowl")],
                         "key", "model", "now", 0.7, {}, "tok", summary)

    msg = mock_tg.call_args.args[2]
    assert "✅" in msg, f"superfood positive marker missing from: {msg}"
    assert "blueberry" in msg.lower()
    assert "⚠️" not in msg, f"avoid warning must NOT appear for superfood: {msg}"


def test_viome_verdict_suppressed_for_minor():
    """Viome guidance (avoid/superfood) must NOT appear in messages to minors."""
    db, captured = _db_capture([
        (200, {"id": "food-uuid", "status": "inserted"}),
        (200, []),
    ])
    from monitor.inbox_drain import _process_cluster
    summary = {"fetched": 0, "clustered": 0, "written": 0, "staged": 0, "failed": 0, "errors": []}

    minor_item = {**_item("r1"), "chat_id": 42}
    avoid_verdict = [{"item": "papaya", "effective_classification": "avoid", "reason": "fructose"}]

    with patch("monitor.inbox_drain.vision_extract", return_value=_FOOD_EXTRACTION), \
         patch("monitor.inbox_drain.lookup_food_reference", return_value=None), \
         patch("monitor.inbox_drain.lookup_viome_verdicts", return_value=avoid_verdict), \
         patch("monitor.inbox_drain.telegram_send", return_value=4242) as mock_tg:
        _process_cluster(db, [minor_item], "key", "model", "now", 0.7,
                         {"42": True}, "tok", summary)

    msg = mock_tg.call_args.args[2]
    assert "⚠️" not in msg, f"avoid warning must not appear for minors: {msg}"
    assert "AVOID" not in msg


def test_food_reference_candidates_caption_first():
    """Caption is the first candidate for food_reference lookup.
    Ensures brand names typed by the user (e.g. 'Hooray') are tried before vision description."""
    from monitor.inbox_drain import _food_reference_candidates
    food_item = {
        "description": "strawberry protein shake",
        "foods": [{"name": "strawberry", "amount": 1, "unit": "cup", "calories": 50}],
    }
    candidates = _food_reference_candidates("Hooray", food_item)
    assert candidates[0] == "Hooray", f"caption must be first candidate; got: {candidates}"
    assert "strawberry protein shake" in candidates
    assert "strawberry" in candidates
    # No duplicates (case-insensitive)
    lower = [c.lower() for c in candidates]
    assert len(lower) == len(set(lower))


def test_learn_from_past_offer_when_no_reference():
    """When no food_reference match but ≥2 past confirmed logs, offer to promote."""
    db, captured = _db_capture([
        (200, {"id": "food-uuid", "status": "inserted"}),
        (200, []),
    ])
    from monitor.inbox_drain import _process_cluster
    summary = {"fetched": 0, "clustered": 0, "written": 0, "staged": 0, "failed": 0, "errors": []}
    cluster = [_item("r1", caption="papaya salad")]

    past_macros = {"calories": 185, "protein_g": 2.5, "carbs_g": 30.0, "fat_g": 1.0, "count": 3}

    with patch("monitor.inbox_drain.vision_extract", return_value=_FOOD_EXTRACTION), \
         patch("monitor.inbox_drain.lookup_food_reference", return_value=None), \
         patch("monitor.inbox_drain.lookup_viome_verdicts", return_value=[]), \
         patch("monitor.inbox_drain.lookup_past_food_macros", return_value=past_macros) as mock_past, \
         patch("monitor.inbox_drain.telegram_send", return_value=4242) as mock_tg:
        _process_cluster(db, cluster, "key", "model", "now", 0.7, {}, "tok", summary,
                         learn_min_logs=2)

    msg = mock_tg.call_args.args[2]
    assert "💡" in msg, f"learn offer missing from: {msg}"
    assert "3" in msg, f"count ({past_macros['count']}) not in offer: {msg}"
    # learn_min_logs was forwarded (mock was called)
    assert mock_past.called


# ── compute_meal_verdict (Step 2 — ingredient-level Viome) ───────────────────

def test_green_curry_decomposes_to_coconut_milk():
    """When vision returns coconut milk in foods[], lookup_viome_verdicts is called
    with 'coconut milk' as a candidate — confirming the decomposition pipeline works."""
    db, _ = _db_capture([
        (200, {"id": "food-uuid", "status": "inserted"}),
        (200, []),   # verdict UPDATE
    ])
    from monitor.inbox_drain import _process_cluster
    summary = {"fetched": 0, "clustered": 0, "written": 0, "staged": 0, "failed": 0, "errors": []}

    green_curry = {
        **_FOOD_EXTRACTION,
        "description": "Thai green curry",
        "foods": [
            {"name": "coconut milk", "amount": 200, "unit": "ml", "calories": 360},
            {"name": "chicken", "amount": 150, "unit": "g", "calories": 250},
        ],
    }

    with patch("monitor.inbox_drain.vision_extract", return_value=green_curry), \
         patch("monitor.inbox_drain.lookup_food_reference", return_value=None), \
         patch("monitor.inbox_drain.lookup_viome_verdicts", return_value=[]) as mock_viome, \
         patch("monitor.inbox_drain.telegram_send", return_value=4242):
        _process_cluster(db, [_item("r1", caption="thai green curry")],
                         "key", "model", "now", 0.7, {}, "tok", summary)

    candidates = mock_viome.call_args[0][1]
    assert "coconut milk" in [c.lower() for c in candidates], (
        f"coconut milk must be a viome candidate; got: {candidates}"
    )


def test_meal_verdict_worst_case_avoid_wins():
    """When verdicts include both avoid and minimize, verdict='avoid' and flags include both."""
    from monitor.inbox_drain import compute_meal_verdict
    verdicts = [
        {"item": "coconut milk", "effective_classification": "avoid", "reason": ""},
        {"item": "onion", "effective_classification": "minimize", "reason": ""},
        {"item": "blueberry", "effective_classification": "superfood", "reason": ""},
    ]
    verdict, flags = compute_meal_verdict(verdicts)
    assert verdict == "avoid"
    assert "coconut milk" in flags
    assert "onion" in flags


def test_meal_verdict_minimize_when_no_avoid():
    """When verdicts have minimize but no avoid, verdict='minimize'."""
    from monitor.inbox_drain import compute_meal_verdict
    verdicts = [
        {"item": "onion", "effective_classification": "minimize", "reason": ""},
        {"item": "garlic", "effective_classification": "minimize", "reason": ""},
    ]
    verdict, flags = compute_meal_verdict(verdicts)
    assert verdict == "minimize"
    assert set(flags) == {"onion", "garlic"}


def test_meal_verdict_clean_when_no_flags():
    """Empty viome verdicts → verdict='clean', flags=[]."""
    from monitor.inbox_drain import compute_meal_verdict
    verdict, flags = compute_meal_verdict([])
    assert verdict == "clean"
    assert flags == []


# ── clarify auto-match + orphan sweep (BACKLOG #15) ───────────────────────────

_CLARIFY_TARGET = {
    "id": "tgt-1",
    "profile_id": "21f69003-46f8-4e1c-a928-b1f694ce4aff",
    "chat_id": 99,
    "caption": "add 1 mystery shake",
    "storage_path": "telegram/p/orig.jpg",
    "stage_reason": "not sure what this is — reply with what you ate",
    "clarify_count": 0,
    "clarify_message_id": 555,
    "staged_review_ids": ["rev-1"],
    "status": "staged",
    "processed_at": "2026-06-07T10:00:00+00:00",
}


def _clarify_db(pending_targets: list[dict]) -> MagicMock:
    db = MagicMock()
    db.select.return_value = pending_targets
    db.update.return_value = []
    return db


def test_absorb_clarify_match_rewrites_cluster_and_retires():
    """Confident judged answer → cluster rewritten as the clarification, staged
    target + its review rows retired, clarify_count bumped on the new rows."""
    from monitor.inbox_drain import absorb_pending_clarify
    db = _clarify_db([dict(_CLARIFY_TARGET)])
    cluster = [_item("m1", kind="unknown", caption="it was a banana bread shake")]
    with patch("monitor.inbox_drain._judge_json",
               return_value={"is_answer": True, "confidence": 0.92}) as judge:
        assert absorb_pending_clarify(db, "key", "model", cluster, 900, 0.7) is True
    judge.assert_called_once()
    assert "[clarification: it was a banana bread shake]" in cluster[0]["caption"]
    assert cluster[0]["caption"].startswith("add 1 mystery shake")
    assert cluster[0]["storage_path"] == "telegram/p/orig.jpg"
    bodies = [c.args[2] if len(c.args) > 2 else c.kwargs.get("body", c.args[-1])
              for c in db.update.call_args_list]
    tables = [c.args[0] for c in db.update.call_args_list]
    assert "stg_food_log_review" in tables  # review rows retired
    stg_body = bodies[tables.index("stg_food_log_review")]
    assert stg_body["status"] == "merged"
    # target retired done + clarify_message_id cleared
    retire = [b for t, b in zip(tables, bodies) if t == "media_inbox" and b.get("status") == "done"]
    assert retire and retire[0]["clarify_message_id"] is None
    # clarify_count bumped on the absorbed rows
    bumps = [b for t, b in zip(tables, bodies) if t == "media_inbox" and "clarify_count" in b]
    assert bumps and bumps[0]["clarify_count"] == 1


def test_absorb_clarify_not_an_answer_leaves_cluster():
    """Judge says not-an-answer → independent processing, nothing retired."""
    from monitor.inbox_drain import absorb_pending_clarify
    db = _clarify_db([dict(_CLARIFY_TARGET)])
    cluster = [_item("m1", kind="unknown", caption="ate a cookie too")]
    with patch("monitor.inbox_drain._judge_json",
               return_value={"is_answer": False, "confidence": 0.9}):
        assert absorb_pending_clarify(db, "key", "model", cluster, 900, 0.7) is False
    assert cluster[0]["caption"] == "ate a cookie too"
    db.update.assert_not_called()


def test_absorb_clarify_low_confidence_leaves_cluster():
    from monitor.inbox_drain import absorb_pending_clarify
    db = _clarify_db([dict(_CLARIFY_TARGET)])
    cluster = [_item("m1", kind="unknown", caption="banana bread")]
    with patch("monitor.inbox_drain._judge_json",
               return_value={"is_answer": True, "confidence": 0.4}):
        assert absorb_pending_clarify(db, "key", "model", cluster, 900, 0.7) is False
    db.update.assert_not_called()


def test_absorb_clarify_no_pending_skips_judge():
    """No staged row in the window → no LLM call at all."""
    from monitor.inbox_drain import absorb_pending_clarify
    db = _clarify_db([])
    cluster = [_item("m1", kind="unknown", caption="banana bread")]
    with patch("monitor.inbox_drain._judge_json") as judge:
        assert absorb_pending_clarify(db, "key", "model", cluster, 900, 0.7) is False
    judge.assert_not_called()


def test_absorb_clarify_photo_cluster_skipped():
    """Photo messages never absorb — Layer 1 is text-only (Layer 2 covers photos)."""
    from monitor.inbox_drain import absorb_pending_clarify
    db = _clarify_db([dict(_CLARIFY_TARGET)])
    row = _item("m1", kind="unknown", caption="x")
    row["storage_path"] = "telegram/p/new.jpg"
    with patch("monitor.inbox_drain._judge_json") as judge:
        assert absorb_pending_clarify(db, "key", "model", [row], 900, 0.7) is False
    judge.assert_not_called()
    db.select.assert_not_called()


def test_absorb_clarify_reply_path_not_reabsorbed():
    """A webhook reply re-queue (caption already carries [clarification:…]) is left alone."""
    from monitor.inbox_drain import absorb_pending_clarify
    db = _clarify_db([dict(_CLARIFY_TARGET)])
    cluster = [_item("m1", kind="unknown",
                     caption="orig\n[clarification: from the reply path]")]
    assert absorb_pending_clarify(db, "key", "model", cluster, 900, 0.7) is False
    db.select.assert_not_called()


def test_absorb_clarify_round_cap_respected():
    from monitor.inbox_drain import absorb_pending_clarify
    capped = dict(_CLARIFY_TARGET, clarify_count=2)
    db = _clarify_db([capped])
    cluster = [_item("m1", kind="unknown", caption="banana bread")]
    with patch("monitor.inbox_drain._judge_json") as judge:
        assert absorb_pending_clarify(db, "key", "model", cluster, 900, 0.7) is False
    judge.assert_not_called()


def _sweep_db(staged: list[dict], foods: list[dict], sups: list[dict]) -> MagicMock:
    db = MagicMock()

    def _select(table, **kw):
        if table == "media_inbox":
            return staged
        if table == "food_logs":
            return foods
        if table == "supplement_intake_logs":
            return sups
        if table == "supplements":
            return [{"id": "supp-def-1", "display_name": "Magnesium Bisglycinate"}]
        return []

    db.select.side_effect = _select
    db.update.return_value = []
    return db


_SWEEP_CFG = {"clarify.orphan_supersede_window_sec": 1800, "clarify.match_min_conf": 0.7}


def test_sweep_orphan_retires_on_match():
    """Staged orphan + matching prod log within the window → retired with result_ref."""
    from monitor.inbox_drain import sweep_staged_orphans
    staged = [dict(_CLARIFY_TARGET)]
    foods = [{"id": "food-1", "description": "Lactose free strawberry shake 340ml",
              "logged_at": "2026-06-07T10:05:00+00:00"}]
    db = _sweep_db(staged, foods, [])
    summary: dict = {}
    with patch("monitor.inbox_drain._judge_json",
               return_value={"match_id": "food-1", "confidence": 0.9}):
        assert sweep_staged_orphans(db, "key", "model", _SWEEP_CFG, summary) == 1
    assert summary["orphans_retired"] == 1
    tables = [c.args[0] for c in db.update.call_args_list]
    bodies = [c.args[2] if len(c.args) > 2 else c.kwargs.get("body", c.args[-1])
              for c in db.update.call_args_list]
    assert "stg_food_log_review" in tables
    done = [b for t, b in zip(tables, bodies) if t == "media_inbox" and b.get("status") == "done"]
    assert done and done[0]["result_ref"] == "food-1"


def test_sweep_orphan_no_candidates_skips_llm():
    """Nothing logged after staging → no LLM call, orphan stays."""
    from monitor.inbox_drain import sweep_staged_orphans
    db = _sweep_db([dict(_CLARIFY_TARGET)], [], [])
    with patch("monitor.inbox_drain._judge_json") as judge:
        assert sweep_staged_orphans(db, "key", "model", _SWEEP_CFG, {}) == 0
    judge.assert_not_called()
    db.update.assert_not_called()


def test_sweep_orphan_outside_window_excluded():
    """A prod log AFTER the supersede window is not even offered to the judge."""
    from monitor.inbox_drain import sweep_staged_orphans
    late = [{"id": "food-1", "description": "shake",
             "logged_at": "2026-06-07T12:00:00+00:00"}]  # 2h later > 1800s window
    db = _sweep_db([dict(_CLARIFY_TARGET)], late, [])
    with patch("monitor.inbox_drain._judge_json") as judge:
        assert sweep_staged_orphans(db, "key", "model", _SWEEP_CFG, {}) == 0
    judge.assert_not_called()


def test_sweep_orphan_low_confidence_leaves():
    from monitor.inbox_drain import sweep_staged_orphans
    foods = [{"id": "food-1", "description": "shake",
              "logged_at": "2026-06-07T10:05:00+00:00"}]
    db = _sweep_db([dict(_CLARIFY_TARGET)], foods, [])
    with patch("monitor.inbox_drain._judge_json",
               return_value={"match_id": "food-1", "confidence": 0.3}):
        assert sweep_staged_orphans(db, "key", "model", _SWEEP_CFG, {}) == 0
    db.update.assert_not_called()


def test_sweep_orphan_hallucinated_match_id_ignored():
    """A judge match_id not in the candidate set is discarded."""
    from monitor.inbox_drain import sweep_staged_orphans
    foods = [{"id": "food-1", "description": "shake",
              "logged_at": "2026-06-07T10:05:00+00:00"}]
    db = _sweep_db([dict(_CLARIFY_TARGET)], foods, [])
    with patch("monitor.inbox_drain._judge_json",
               return_value={"match_id": "food-999", "confidence": 0.95}):
        assert sweep_staged_orphans(db, "key", "model", _SWEEP_CFG, {}) == 0
    db.update.assert_not_called()


def test_sweep_orphan_supplement_candidate_matches():
    """Supplement orphans match against supplement_intake_logs (display_name resolved)."""
    from monitor.inbox_drain import sweep_staged_orphans
    staged = [dict(_CLARIFY_TARGET, caption="took a supplemen",
                   stage_reason="not sure which supplement — reply with the name",
                   staged_review_ids=None)]
    sups = [{"id": "sil-1", "supplement_id": "supp-def-1",
             "taken_at": "2026-06-07T10:01:00+00:00"}]
    db = _sweep_db(staged, [], sups)
    with patch("monitor.inbox_drain._judge_json",
               return_value={"match_id": "sil-1", "confidence": 0.88}):
        assert sweep_staged_orphans(db, "key", "model", _SWEEP_CFG, {}) == 1
    tables = [c.args[0] for c in db.update.call_args_list]
    assert "stg_food_log_review" not in tables  # no review ids on this orphan


# ── food→supplement bridge ─────────────────────────────────────────────────────

from monitor.inbox_drain import (  # noqa: E402
    bridge_food_supplements,
    match_regimen_supplements_in_text,
)

_BRIDGE_REGIMEN = [
    {"supplement_id": "cre", "name": "creatine", "display_name": "Creatine Monohydrate"},
    {"supplement_id": "mbg", "name": "magnesium_bisglycinate", "display_name": "Magnesium Bisglycinate"},
    {"supplement_id": "mct", "name": "magnesium_citrate", "display_name": "Magnesium Citrate"},
    {"supplement_id": "nac", "name": "nac", "display_name": "NAC"},
    {"supplement_id": "k2", "name": "vitamin_k2_mk7", "display_name": "Vitamin K2 (MK-7)"},
]


def test_match_creatine_in_shake_glutamine_ignored():
    """Creatine (in regimen) matches; glutamine (NOT in regimen) is never invented."""
    m = match_regimen_supplements_in_text(
        "TWT strawberry protein shake with creatine and glutamine", _BRIDGE_REGIMEN)
    assert {x["supplement_id"] for x in m} == {"cre"}


def test_match_bare_magnesium_is_ambiguous_no_match():
    """A bare 'magnesium' matches NEITHER citrate nor bisglycinate (full phrase needed)."""
    assert match_regimen_supplements_in_text("took some magnesium tonight", _BRIDGE_REGIMEN) == []


def test_match_explicit_magnesium_citrate():
    """The explicit differentiator resolves to exactly that variant."""
    m = match_regimen_supplements_in_text("magnesium citrate 200mg", _BRIDGE_REGIMEN)
    assert {x["supplement_id"] for x in m} == {"mct"}


def test_match_nac_not_substring_of_snack():
    """Whole-token match: 'nac' must not fire inside 'snack'."""
    assert match_regimen_supplements_in_text("had a snack and a smoothie", _BRIDGE_REGIMEN) == []


def test_match_vitamin_k2_via_paren_stripped_display():
    """Parenthetical-stripped display name lets 'vitamin k2' trigger 'Vitamin K2 (MK-7)'."""
    m = match_regimen_supplements_in_text("vitamin k2 in the morning", _BRIDGE_REGIMEN)
    assert {x["supplement_id"] for x in m} == {"k2"}


def test_bridge_logs_new_and_skips_already_logged():
    """Bridge writes only matches not already logged today; pins noon-UTC; source telegram."""
    db = MagicMock()
    db.select.side_effect = [
        # fetch_active_regimen_supplements → regimens, then supplements
        [{"supplement_id": "cre", "dose_amount": 5, "dose_unit": "g"},
         {"supplement_id": "mct", "dose_amount": 200, "dose_unit": "mg"}],
        [{"id": "cre", "name": "creatine", "display_name": "Creatine Monohydrate"},
         {"id": "mct", "name": "magnesium_citrate", "display_name": "Magnesium Citrate"}],
        # existing intakes today: magnesium already logged (e.g. via 📝 menu)
        [{"supplement_id": "mct"}],
    ]
    db.rpc.return_value = {"status": "inserted", "id": "x"}
    out = bridge_food_supplements(
        db, _PC_PROFILE_ID, "2026-06-14",
        "protein shake with creatine and magnesium citrate")
    assert out == ["Creatine Monohydrate"]              # mct skipped — already logged
    assert db.rpc.call_count == 1
    args = db.rpc.call_args[0][1]
    assert args["p_supplement_id"] == "cre"
    assert args["p_taken_at"] == "2026-06-14T12:00:00Z"  # menu-matching noon-UTC pin
    assert args["p_source"] == "telegram"
    assert args["p_force_stage"] is False
    assert args["p_dose_amount"] == 5 and args["p_dose_unit"] == "g"


def test_bridge_no_regimen_returns_empty():
    """No active regimen → nothing matched, no writes."""
    db = MagicMock()
    db.select.side_effect = [[]]                          # empty regimen
    assert bridge_food_supplements(db, _PC_PROFILE_ID, "2026-06-14", "shake with creatine") == []
    db.rpc.assert_not_called()


def test_process_cluster_appends_bridge_confirmation():
    """A food write with a bridged supplement appends the 'Also logged' line to the confirmation."""
    db, _captured = _db_capture([(200, {"id": "f1", "status": "inserted"}), (200, [])])
    from monitor.inbox_drain import _process_cluster
    summary = {"fetched": 0, "clustered": 0, "written": 0, "staged": 0, "failed": 0, "errors": []}
    cluster = [_item("r1", caption="protein shake with creatine")]
    sent: dict = {}

    def _send(token, chat, msg, **kw):
        sent["msg"] = msg
        return 4242

    with patch("monitor.inbox_drain.vision_extract",
               return_value={"description": "protein shake", "calories": 320,
                             "protein_g": 40, "confidence": 0.9}), \
         patch("monitor.inbox_drain.lookup_food_reference", return_value=None), \
         patch("monitor.inbox_drain.lookup_past_food_macros", return_value=None), \
         patch("monitor.inbox_drain.lookup_viome_verdicts", return_value=[]), \
         patch("monitor.inbox_drain.bridge_food_supplements",
               return_value=["Creatine Monohydrate"]), \
         patch("monitor.inbox_drain.telegram_send", side_effect=_send):
        _process_cluster(db, cluster, "key", "model", "now", 0.7, {}, "tok", summary,
                         today="2026-06-14")

    assert "💊 Also logged: Creatine Monohydrate" in sent["msg"]
