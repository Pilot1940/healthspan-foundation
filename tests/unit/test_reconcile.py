"""Unit tests for monitor.reconcile — age-aware window computation.

Tests the per-profile window logic (_profile_window, _last_sync_hours_ago,
_get_config_int) using mock DB connections. No live DB or WHOOP API required.

The core requirement: reconcile widens the sync window when a profile's last
successful sync is older than the default, so no data is silently missed.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from monitor.reconcile import (
    _BUFFER_HOURS,
    _FALLBACK_DEFAULT_HOURS,
    _FALLBACK_MAX_WIDEN,
    _get_config_int,
    _last_sync_hours_ago,
    _profile_window,
)


# ---------------------------------------------------------------------------
# Mock DB helpers
# ---------------------------------------------------------------------------

class _MockCursor:
    def __init__(self, rows):
        self._rows = rows  # list of row tuples; fetchone returns rows[0]
        self._called = []

    def execute(self, sql, params=()):
        self._called.append((sql, params))

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _MockConn:
    def __init__(self, row=None):
        self._row = (row,) if row is not None else None
        self.cursor_obj = None

    def cursor(self):
        self.cursor_obj = _MockCursor([self._row] if self._row else [])
        return self.cursor_obj


class _MockConnMulti:
    """Returns different rows per cursor call — for multi-query tests."""
    def __init__(self, rows):
        self._rows = list(rows)
        self._idx = 0

    def cursor(self):
        row = self._rows[self._idx] if self._idx < len(self._rows) else None
        self._idx += 1
        return _MockCursor([row] if row else [])


# ---------------------------------------------------------------------------
# _get_config_int
# ---------------------------------------------------------------------------

def test_get_config_int_returns_value():
    conn = _MockConn(72)
    assert _get_config_int(conn, "push.reconcile_default_hours", 48) == 72


def test_get_config_int_falls_back_on_none_row():
    conn = _MockConn()  # no row
    result = _get_config_int(conn, "push.reconcile_default_hours", 48)
    assert result == 48


def test_get_config_int_falls_back_on_null_value():
    class _NullCursor:
        def execute(self, *_): pass
        def fetchone(self): return (None,)
    class _NullConn:
        def cursor(self): return _NullCursor()
    assert _get_config_int(_NullConn(), "push.reconcile_default_hours", 48) == 48


# ---------------------------------------------------------------------------
# _last_sync_hours_ago
# ---------------------------------------------------------------------------

def test_last_sync_hours_ago_returns_float():
    conn = _MockConn(36.5)
    result = _last_sync_hours_ago(conn, "fake-profile-id")
    assert result == 36.5


def test_last_sync_hours_ago_returns_none_when_no_row():
    conn = _MockConn()  # empty result
    result = _last_sync_hours_ago(conn, "fake-profile-id")
    assert result is None


# ---------------------------------------------------------------------------
# _profile_window — core age-aware logic
# ---------------------------------------------------------------------------

def test_profile_window_widens_on_stale_sync():
    """reconcile widens on stale sync: last sync 72h ago → window > 48."""
    conn = _MockConn(72.0)  # last sync 72h ago
    window = _profile_window(conn, "pid", default_hours=48, max_widen=168)
    expected = 72 + _BUFFER_HOURS  # ceil(72.0) + 2 = 74, which is > 48
    assert window == expected
    assert window > 48


def test_profile_window_uses_default_for_recent_sync():
    """Recent sync (10h): window stays at default 48h."""
    conn = _MockConn(10.0)
    window = _profile_window(conn, "pid", default_hours=48, max_widen=168)
    assert window == 48  # 10 + 2 = 12 < 48 → use default


def test_profile_window_caps_at_max_widen():
    """Very old sync (200h): window capped at max_widen=168."""
    conn = _MockConn(200.0)
    window = _profile_window(conn, "pid", default_hours=48, max_widen=168)
    assert window == 168


def test_profile_window_never_synced_uses_max_widen():
    """Profile with no sync history gets max_widen (not unbounded backfill)."""
    conn = _MockConn()  # None → never synced
    window = _profile_window(conn, "pid", default_hours=48, max_widen=168)
    assert window == 168


def test_profile_window_exactly_at_default_boundary():
    """Sync exactly default_hours ago: window = default + buffer (widens by buffer)."""
    conn = _MockConn(48.0)
    window = _profile_window(conn, "pid", default_hours=48, max_widen=168)
    assert window == 48 + _BUFFER_HOURS  # 50 > 48 → widen


def test_profile_window_buffer_ensures_overlap():
    """Buffer hours ensure the window always overlaps the last sync by _BUFFER_HOURS."""
    conn = _MockConn(50.0)  # 50h ago
    window = _profile_window(conn, "pid", default_hours=48, max_widen=168)
    assert window == 50 + _BUFFER_HOURS


def test_profile_window_fallback_constants_are_sane():
    """Sanity check: default fallbacks are within expected operational range."""
    assert _FALLBACK_DEFAULT_HOURS == 48
    assert _FALLBACK_MAX_WIDEN == 168
    assert _BUFFER_HOURS >= 1
