"""Unit tests for the V3-8 reusable WHOOP refresh helpers (window parsing + multi-profile
refresh with per-profile isolation) + the BACKLOG #20 token-rotation race recovery.
DB + network mocked."""
from __future__ import annotations

import urllib.error
from datetime import datetime, timedelta, timezone
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


def _http_400():
    return urllib.error.HTTPError("https://whoop/token", 400, "Bad Request", None, None)


class TestTokenRaceRecovery:
    """BACKLOG #20: a refresh 400 (rotated-out token) re-reads whoop_tokens and either
    uses the race winner's fresh access token or retries ONCE with the rotated-in
    refresh token. Non-400s and unrecoverable states re-raise."""

    @staticmethod
    def _conn(rows):
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value = cur
        cur.fetchone.side_effect = rows
        return conn, cur

    @staticmethod
    def _expired_row(refresh="old-rt", access="stale-at"):
        return {"access_token": access, "refresh_token": refresh,
                "expires_at": datetime.now(timezone.utc) - timedelta(hours=1),
                "whoop_user_id": "w1"}

    def test_race_uses_winners_access_token(self):
        winner = {"access_token": "winner-at", "refresh_token": "new-rt",
                  "expires_at": datetime.now(timezone.utc) + timedelta(hours=1)}
        conn, _ = self._conn([self._expired_row(), winner])
        with patch.object(ws, "_post_refresh", side_effect=_http_400()) as pr:
            tok = ws._get_token_from_db(conn, "p1", {"WHOOP_CLIENT_ID": "c", "WHOOP_CLIENT_SECRET": "s"})
        assert tok == "winner-at"
        pr.assert_called_once()                          # no second refresh attempt needed

    def test_race_retries_once_with_rotated_token(self):
        rotated = self._expired_row(refresh="new-rt")    # winner stored a new refresh token,
        conn, cur = self._conn([self._expired_row(), rotated])  # but its access already aged out
        fresh = {"access_token": "fresh-at", "refresh_token": "next-rt", "expires_in": 3600}
        with patch.object(ws, "_post_refresh", side_effect=[_http_400(), fresh]) as pr:
            tok = ws._get_token_from_db(conn, "p1", {"WHOOP_CLIENT_ID": "c", "WHOOP_CLIENT_SECRET": "s"})
        assert tok == "fresh-at"
        assert pr.call_count == 2
        assert pr.call_args_list[1][0][0] == "new-rt"    # retry used the rotated-in token
        conn.commit.assert_called()                      # fresh pair stored back

    def test_race_unrecoverable_reraises(self):
        # Re-read shows the SAME refresh token still expired → nothing rotated in; re-raise.
        conn, _ = self._conn([self._expired_row(), self._expired_row()])
        with patch.object(ws, "_post_refresh", side_effect=_http_400()):
            try:
                ws._get_token_from_db(conn, "p1", {"WHOOP_CLIENT_ID": "c", "WHOOP_CLIENT_SECRET": "s"})
                raise AssertionError("expected HTTPError")
            except urllib.error.HTTPError as e:
                assert e.code == 400

    def test_non_400_reraises_without_reread(self):
        conn, cur = self._conn([self._expired_row()])
        err = urllib.error.HTTPError("https://whoop/token", 500, "boom", None, None)
        with patch.object(ws, "_post_refresh", side_effect=err):
            try:
                ws._get_token_from_db(conn, "p1", {"WHOOP_CLIENT_ID": "c", "WHOOP_CLIENT_SECRET": "s"})
                raise AssertionError("expected HTTPError")
            except urllib.error.HTTPError as e:
                assert e.code == 500
        assert cur.fetchone.call_count == 1              # no race re-read on a non-400


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
