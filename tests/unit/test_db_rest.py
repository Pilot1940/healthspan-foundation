"""Unit tests for lib/db_rest.py — the httpx PostgREST client (App / non-maintainer path).

All mocked via httpx.MockTransport / monkeypatch — no network, no DB.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from lib.db_rest import DbRest, sign_in


def _db(handler) -> DbRest:
    """DbRest wired to a MockTransport handler(request)->Response."""
    db = DbRest.__new__(DbRest)
    db._base = "https://test.supabase.co"
    db._anon_key = "anon"
    db._jwt = "jwt"
    db._client = httpx.Client(transport=httpx.MockTransport(handler))
    return db


def _resp(status: int, body) -> httpx.Response:
    return httpx.Response(status, content=json.dumps(body).encode(),
                          headers={"content-type": "application/json"})


# ── headers ─────────────────────────────────────────────────────────────────

def test_headers_carry_apikey_and_bearer():
    seen = {}

    def h(req):
        seen["apikey"] = req.headers.get("apikey")
        seen["auth"] = req.headers.get("authorization")
        return _resp(200, [])

    _db(h).select("t")
    assert seen["apikey"] == "anon"
    assert seen["auth"] == "Bearer jwt"


# ── select ────────────────────────────────────────────────────────────────────

def test_select_passes_filters_and_returns_rows():
    captured = {}

    def h(req):
        captured["url"] = str(req.url)
        return _resp(200, [{"id": "x"}])

    rows = _db(h).select("food_logs", select="id", filters={"profile_id": "eq.p1"}, limit=5, order="logged_at")
    assert rows == [{"id": "x"}]
    assert "profile_id=eq.p1" in captured["url"]
    assert "select=id" in captured["url"]
    assert "limit=5" in captured["url"]
    assert "order=logged_at" in captured["url"]


def test_select_raises_on_error_status():
    with pytest.raises(httpx.HTTPStatusError):
        _db(lambda req: _resp(401, {"message": "JWT expired"})).select("t")


# ── insert ────────────────────────────────────────────────────────────────────

def test_insert_returns_first_row_with_prefer_return():
    def h(req):
        assert req.headers.get("prefer") == "return=representation"
        return _resp(201, [{"id": "new-1"}])

    assert _db(h).insert("food_logs", {"a": 1}) == {"id": "new-1"}


def test_insert_empty_representation_returns_none():
    assert _db(lambda req: _resp(201, [])).insert("food_logs", {"a": 1}) is None


def test_insert_no_return_skips_body_parse():
    # 201 with empty body + prefer_return=False must not blow up on .json()
    assert _db(lambda req: httpx.Response(201, content=b"")).insert("t", {"a": 1}, prefer_return=False) is None


def test_insert_raises_on_error():
    with pytest.raises(httpx.HTTPStatusError):
        _db(lambda req: _resp(403, {"message": "RLS"})).insert("t", {"a": 1})


# ── update / claim ─────────────────────────────────────────────────────────────

def test_update_default_returns_empty_list():
    assert _db(lambda req: httpx.Response(204, content=b"")).update("t", {"id": "eq.1"}, {"x": 2}) == []


def test_update_prefer_return_parses_rows():
    rows = _db(lambda req: _resp(200, [{"id": "1", "x": 2}])).update(
        "t", {"id": "eq.1"}, {"x": 2}, prefer_return=True)
    assert rows == [{"id": "1", "x": 2}]


def test_claim_inbox_item_true_when_row_updated():
    assert _db(lambda req: _resp(200, [{"id": "m1"}])).claim_inbox_item("m1") is True


def test_claim_inbox_item_false_when_already_claimed():
    # filtered PATCH matched nothing (another worker won) → no rows → not claimed
    assert _db(lambda req: _resp(200, [])).claim_inbox_item("m1") is False


# ── rpc ────────────────────────────────────────────────────────────────────────

def test_rpc_returns_json():
    assert _db(lambda req: _resp(200, {"status": "inserted", "id": "x"})).rpc(
        "maintainer_ingest_food", {"p_a": 1}) == {"status": "inserted", "id": "x"}


def test_rpc_empty_body_returns_none():
    assert _db(lambda req: httpx.Response(200, content=b"")).rpc("fn", {}) is None


def test_rpc_raises_on_error():
    with pytest.raises(httpx.HTTPStatusError):
        _db(lambda req: _resp(500, {"message": "boom"})).rpc("fn", {})


# ── sign_in ─────────────────────────────────────────────────────────────────

def test_sign_in_returns_access_token():
    resp = MagicMock()
    resp.json.return_value = {"access_token": "JWT123"}
    resp.raise_for_status = MagicMock()
    with patch("lib.db_rest.httpx.post", return_value=resp) as p:
        tok = sign_in("https://x.supabase.co/", "anon", "e@x.com", "pw")
    assert tok == "JWT123"
    # password grant, anon apikey header
    _, kwargs = p.call_args
    assert kwargs["params"]["grant_type"] == "password"
    assert kwargs["headers"]["apikey"] == "anon"


def test_sign_in_raises_on_bad_credentials():
    resp = MagicMock()
    resp.raise_for_status.side_effect = httpx.HTTPStatusError("400", request=MagicMock(), response=MagicMock())
    with patch("lib.db_rest.httpx.post", return_value=resp):
        with pytest.raises(httpx.HTTPStatusError):
            sign_in("https://x.supabase.co", "anon", "e@x.com", "wrong")
