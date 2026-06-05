"""Unit tests for lib/db.py — supabase_client authentication + lazy psycopg2 (v3.2).

All external deps are mocked: a fake `supabase` module is injected into sys.modules, and
psycopg2 absence is simulated by setting sys.modules['psycopg2'] = None.
"""
from __future__ import annotations

import copy
import sys
import types
from unittest.mock import MagicMock, patch

import pytest

from lib import db

CFG_SUPA = {
    "connection": {
        "mode": "supabase_client",
        "supabase_client": {
            "supabase_url": "https://x.supabase.co",
            "supabase_anon_key": "anon-key",
            "auth_email": "dea@chitalkar.com",
            "auth_password": "s3cret",
        },
    }
}
CFG_DIRECT = {
    "connection": {
        "mode": "direct_role",
        "direct_role": {"db_url": "postgresql://u:p@h:6543/postgres", "auth_user_id": "uid-1"},
    }
}


def _fake_supabase(*, session_token="jwt-abc", raise_on_signin=None):
    """Return (fake `supabase` module, mock client)."""
    client = MagicMock()
    if raise_on_signin is not None:
        client.auth.sign_in_with_password.side_effect = raise_on_signin
    else:
        res = MagicMock()
        res.session = None if session_token is None else MagicMock(access_token=session_token)
        client.auth.sign_in_with_password.return_value = res
    mod = types.ModuleType("supabase")
    mod.create_client = MagicMock(return_value=client)
    return mod, client


class TestSupabaseAuth:
    def test_signs_in_with_config_creds_and_returns_authed_client(self):
        mod, client = _fake_supabase()
        with patch.dict(sys.modules, {"supabase": mod}):
            handle, driver = db.get_app_connection(CFG_SUPA)
        assert driver == "supabase" and handle is client
        # signed in BEFORE returning, with the exact config creds
        client.auth.sign_in_with_password.assert_called_once_with(
            {"email": "dea@chitalkar.com", "password": "s3cret"}
        )
        mod.create_client.assert_called_once_with("https://x.supabase.co", "anon-key")

    def test_missing_password_raises_actionable_error_no_signin(self):
        cfg = copy.deepcopy(CFG_SUPA)
        del cfg["connection"]["supabase_client"]["auth_password"]
        mod, client = _fake_supabase()
        with patch.dict(sys.modules, {"supabase": mod}):
            with pytest.raises(ValueError, match=r"auth_password required for App mode"):
                db.get_app_connection(cfg)
        # never created a client / never signed in → never an anon fall-through
        mod.create_client.assert_not_called()
        client.auth.sign_in_with_password.assert_not_called()

    def test_missing_email_raises(self):
        cfg = copy.deepcopy(CFG_SUPA)
        del cfg["connection"]["supabase_client"]["auth_email"]
        mod, _ = _fake_supabase()
        with patch.dict(sys.modules, {"supabase": mod}):
            with pytest.raises(ValueError, match=r"auth_email required for App mode"):
                db.get_app_connection(cfg)

    def test_failed_auth_raises_never_returns_anon(self):
        mod, client = _fake_supabase(raise_on_signin=Exception("invalid login credentials"))
        with patch.dict(sys.modules, {"supabase": mod}):
            with pytest.raises(RuntimeError, match=r"sign-in failed"):
                db.get_app_connection(CFG_SUPA)

    def test_no_session_returned_raises(self):
        mod, client = _fake_supabase(session_token=None)
        with patch.dict(sys.modules, {"supabase": mod}):
            with pytest.raises(RuntimeError, match=r"no session/JWT"):
                db.get_app_connection(CFG_SUPA)


class TestLazyPsycopg2:
    def test_supabase_path_works_without_psycopg2(self):
        mod, client = _fake_supabase()
        # psycopg2 absent: the supabase path must not touch it
        with patch.dict(sys.modules, {"supabase": mod, "psycopg2": None}):
            handle, driver = db.get_app_connection(CFG_SUPA)
        assert driver == "supabase" and handle is client

    def test_direct_role_without_psycopg2_raises_clear(self):
        with patch.dict(sys.modules, {"psycopg2": None}):
            with pytest.raises(RuntimeError, match=r"pip install psycopg2-binary"):
                db.get_app_connection(CFG_DIRECT)

    def test_get_conn_without_psycopg2_raises_clear(self):
        with patch.dict(sys.modules, {"psycopg2": None}):
            with pytest.raises(RuntimeError, match=r"pip install psycopg2-binary"):
                db.get_conn()
