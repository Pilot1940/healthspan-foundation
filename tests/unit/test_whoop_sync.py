"""Unit tests for the V3-8 reusable WHOOP refresh helpers (window parsing + multi-profile
refresh with per-profile isolation). DB + network mocked."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from ingest import whoop_sync as ws

NOW = datetime(2026, 6, 4, 12, 0, 0, tzinfo=timezone.utc)


class TestParseSince:
    def test_relative_hours(self):
        assert ws._parse_since("48h", NOW) == "2026-06-02T12:00:00Z"

    def test_relative_days(self):
        assert ws._parse_since("2d", NOW) == "2026-06-02T12:00:00Z"

    def test_iso_date_passthrough(self):
        assert ws._parse_since("2026-06-01", NOW) == "2026-06-01T00:00:00Z"

    def test_whitespace_and_case_tolerant(self):
        assert ws._parse_since(" 24H ", NOW) == "2026-06-03T12:00:00Z"


class TestRefreshRecent:
    def test_defaults_to_all_token_profiles_and_isolates_failures(self):
        conn = MagicMock()
        with patch.object(ws, "_token_profiles", return_value=["pA", "pB"]) as tp, \
             patch.object(ws, "sync_profile",
                          side_effect=[[{"table": "whoop_cycles", "upserted": 1}],
                                       RuntimeError("refresh token dead")]) as sp:
            out = ws.refresh_recent(conn=conn, hours=48, env={})
        tp.assert_called_once()
        assert sp.call_count == 2                       # both profiles attempted
        assert out["pA"] == [{"table": "whoop_cycles", "upserted": 1}]
        assert out["pB"] == {"error": "refresh token dead"}  # isolated, not raised
        conn.rollback.assert_called()                   # failed profile rolled back

    def test_explicit_profiles_and_window_passed_through(self):
        conn = MagicMock()
        with patch.object(ws, "_token_profiles") as tp, \
             patch.object(ws, "sync_profile", return_value=[]) as sp:
            ws.refresh_recent(conn=conn, hours=24, profiles=["pX"], env={})
        tp.assert_not_called()                          # explicit list → no DB lookup
        # sync_profile(conn, pid, since_iso, to_iso, env): since is 24h before to
        _, pid, since_iso, to_iso, _env = sp.call_args[0]
        assert pid == "pX" and since_iso.endswith("Z") and to_iso.endswith("Z")

    def test_opens_and_closes_own_connection_when_none_given(self):
        fake = MagicMock()
        with patch.object(ws, "get_conn", return_value=fake) as gc, \
             patch.object(ws, "_load_env", return_value={}), \
             patch.object(ws, "_token_profiles", return_value=[]):
            ws.refresh_recent()
        gc.assert_called_once()
        fake.close.assert_called_once()                 # owns lifecycle → closes it
