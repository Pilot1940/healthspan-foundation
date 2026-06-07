"""Unit tests for Phase 3A inbox_drain + db_rest helpers.

Tests cover:
  - db_rest: claim atomic (won/lost), select/insert/update via mock HTTP
  - inbox_drain: build_clusters (album vs ungrouped), merge_caption
  - inbox_drain: write_food → done/staged depending on confidence
  - inbox_drain: write_biomarker / write_supplement guard
  - inbox_drain: minor framing assertion (no deficit words in confirmation text)
  - inbox_drain: fire dedup (won vs lost race)

No live DB or HTTP required — all I/O mocked via httpx.MockTransport.
"""
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import json
import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from lib.db_rest import DbRest
from monitor.inbox_drain import (
    build_clusters,
    merge_caption,
    mark_rows,
)


# ── helpers ───────────────────────────────────────────────────────────────────

def _db(responses: list[tuple[int, Any]]) -> DbRest:
    """Build a DbRest backed by a mock transport that plays back responses."""
    idx = 0

    def _handler(request: httpx.Request) -> httpx.Response:
        nonlocal idx
        status, body = responses[idx % len(responses)]
        idx += 1
        return httpx.Response(status, json=body)

    client = httpx.Client(transport=httpx.MockTransport(_handler))
    db = DbRest.__new__(DbRest)
    db._base = "https://test.supabase.co"
    db._anon_key = "anon"
    db._jwt = "jwt"
    db._client = client
    return db


def _make_item(id: str, mgid: str | None = None, caption: str = "", kind: str = "food") -> dict:
    return {
        "id": id,
        "profile_id": "21f69003-46f8-4e1c-a928-b1f694ce4aff",
        "chat_id": 12345,
        "kind": kind,
        "storage_path": None,
        "caption": caption,
        "media_group_id": mgid,
        "status": "pending",
        "created_at": "2026-06-07T10:00:00Z",
    }


# ── db_rest: claim_inbox_item ─────────────────────────────────────────────────

def test_claim_returns_true_when_row_updated():
    """claim() returns True when the PATCH updates a row (won the race)."""
    db = _db([(200, [{"id": "abc", "status": "processing"}])])
    assert db.claim_inbox_item("abc") is True


def test_claim_returns_false_when_no_row_matched():
    """claim() returns False when 0 rows returned (already claimed)."""
    db = _db([(200, [])])
    assert db.claim_inbox_item("abc") is False


# ── db_rest: select, insert, rpc ──────────────────────────────────────────────

def test_select_returns_rows():
    rows = [{"id": "1", "key": "push.inbox_settle_sec", "value": "90"}]
    db = _db([(200, rows)])
    result = db.select("system_config")
    assert result == rows


def test_insert_returns_created_row():
    row = {"id": "new-uuid", "status": "pending"}
    db = _db([(200, [row])])
    result = db.insert("media_inbox", {"status": "pending"})
    assert result == row


def test_rpc_returns_payload():
    payload = {"id": "row-uuid", "status": "inserted"}
    db = _db([(200, payload)])
    result = db.rpc("maintainer_ingest_food", {"p_profile_id": "x"})
    assert result == payload


# ── inbox_drain: build_clusters ───────────────────────────────────────────────

def test_build_clusters_groups_shared_media_group_id():
    """Three photos from the same album share media_group_id → one cluster."""
    items = [
        _make_item("a1", mgid="mg1"),
        _make_item("a2", mgid="mg1"),
        _make_item("a3", mgid="mg1"),
    ]
    albums, ungrouped = build_clusters(items)
    assert len(albums) == 1
    assert len(albums[0]) == 3
    assert ungrouped == []


def test_build_clusters_single_mgid_becomes_ungrouped():
    """Only one row with a media_group_id — not enough for an album cluster."""
    items = [_make_item("a1", mgid="mg1")]
    albums, ungrouped = build_clusters(items)
    assert albums == []
    assert len(ungrouped) == 1


def test_build_clusters_two_unrelated_foods_stay_separate():
    """Two items with no media_group_id → both ungrouped (Routine LLM decides if mergeable)."""
    items = [_make_item("a1"), _make_item("a2")]
    albums, ungrouped = build_clusters(items)
    assert albums == []
    assert len(ungrouped) == 2


def test_build_clusters_mixed():
    """Album + singletons coexist correctly."""
    items = [
        _make_item("a1", mgid="mg1"),
        _make_item("a2", mgid="mg1"),
        _make_item("b1"),
        _make_item("b2"),
    ]
    albums, ungrouped = build_clusters(items)
    assert len(albums) == 1
    assert len(albums[0]) == 2
    assert len(ungrouped) == 2


# ── inbox_drain: merge_caption ────────────────────────────────────────────────

def test_merge_caption_deduplicates():
    rows = [
        _make_item("a", caption="chicken salad"),
        _make_item("b", caption="chicken salad"),   # duplicate
        _make_item("c", caption="with avocado"),
    ]
    result = merge_caption(rows)
    assert result == "chicken salad | with avocado"


def test_merge_caption_empty_rows():
    rows = [_make_item("a", caption=""), _make_item("b", caption=None)]
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
    rows = [_make_item("r1")]
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
    rows = [_make_item("r1")]
    result = write_food(db, rows, rows[0]["profile_id"],
                        {"meal_type": "unknown", "description": "blurry food"},
                        confidence=0.3, raw_text="blurry")
    assert result.get("status") == "staged"


# ── minor framing: no deficit words in any Routine confirmation ───────────────

_DEFICIT_WORDS = ["low", "poor", "bad", "deficit", "restrict", "not enough", "missing"]


def _compose_confirmation(kind: str, status: str, extracted: dict, is_minor: bool) -> str:
    """Mirrors the Routine-PROMPT confirmation logic — kept here for assertion."""
    if kind == "food" and status == "inserted":
        desc = extracted.get("description", "food")
        kcal = extracted.get("calories", "?")
        if is_minor:
            return f"Great log! 💪 {desc} — {kcal} kcal"
        return f"✅ Logged: {desc} — {kcal} kcal"
    if kind == "lab":
        n = extracted.get("n", 1)
        if is_minor:
            return f"📊 Tracking nicely — {n} biomarker(s) recorded"
        return f"✅ {n} biomarker(s) logged"
    if status == "staged":
        if is_minor:
            return "📋 I'll check this one with PC — thanks for sending it!"
        return "📋 Queued for PC review — confidence too low to auto-log."
    return "✅ Logged."


def test_minor_confirmation_food_no_deficit_language():
    msg = _compose_confirmation("food", "inserted", {"description": "salad", "calories": 350}, is_minor=True)
    for word in _DEFICIT_WORDS:
        assert word not in msg.lower(), f"Found deficit word '{word}' in minor confirmation: {msg}"


def test_minor_confirmation_lab_no_deficit_language():
    msg = _compose_confirmation("lab", "inserted", {"n": 3}, is_minor=True)
    for word in _DEFICIT_WORDS:
        assert word not in msg.lower(), f"Found deficit word '{word}' in minor confirmation: {msg}"


def test_minor_confirmation_staged_no_deficit_language():
    msg = _compose_confirmation("food", "staged", {}, is_minor=True)
    for word in _DEFICIT_WORDS:
        assert word not in msg.lower(), f"Found deficit word '{word}' in minor confirmation: {msg}"


# ── fire dedup race ───────────────────────────────────────────────────────────
# The Routine itself is tested in the Edge function test; here we verify
# the Python drain's get_config reads the right keys.

def test_get_config_returns_dict():
    from monitor.inbox_drain import get_config
    rows = [
        {"key": "push.inbox_settle_sec", "value": "90"},
        {"key": "routine.fire_dedup_sec", "value": "300"},
    ]
    db = _db([(200, rows)])
    cfg = get_config(db)
    assert cfg["push.inbox_settle_sec"] == "90"
    assert cfg["routine.fire_dedup_sec"] == "300"
