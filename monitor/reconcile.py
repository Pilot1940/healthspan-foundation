"""monitor/reconcile.py — age-aware WHOOP reconcile. Importable + CLI.

Wraps ingest.whoop_sync.refresh_recent with a per-profile age-aware window.
Default window comes from system_config 'push.reconcile_default_hours' (default 48h).
Per profile, if the last successful wearable_sync_log run is older than the default
window, the window widens to cover the gap (capped at push.reconcile_max_widen_hours,
default 168h / 7d). Never triggers an unbounded backfill.

Also runs the prior-cycle refresh (WHOOP emits no cycle.updated webhook — the cycle
row goes stale at ~0 strain until re-pulled at recovery time). This is already handled
inside refresh_recent → sync_cycles.

Returns {profile_id: {"window_hours": int, "results": list | dict | None}}.
One profile's failure never blocks the others.

Usage:
    python -m monitor.reconcile                  # all token-holding profiles
    python -m monitor.reconcile --profile PC     # one profile by name or UUID
    python -m monitor.reconcile --hours 24       # override default window
"""
from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from ingest.whoop_sync import refresh_recent
from lib.db import get_conn, resolve_profile

_FALLBACK_DEFAULT_HOURS = 48
_FALLBACK_MAX_WIDEN = 168
_BUFFER_HOURS = 2  # extra hours added on top of gap to avoid edge-case misses


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def _get_config_int(conn, key: str, default: int) -> int:
    """Read an integer system_config value; fall back to default without raising."""
    cur = conn.cursor()
    cur.execute(
        "SELECT value FROM system_config WHERE key = %s AND is_active", (key,)
    )
    row = cur.fetchone()
    if row and row[0] is not None:
        try:
            return int(row[0])
        except (TypeError, ValueError):
            pass
    return default


# ---------------------------------------------------------------------------
# Per-profile window computation
# ---------------------------------------------------------------------------

def _last_sync_hours_ago(conn, profile_id: str) -> float | None:
    """Hours since this profile's last successful wearable_sync_log run.

    'Successful' = status='success'. Even a run with 0 upserts resets this
    clock (the sync ran and the window is covered). Returns None if no
    successful run exists (profile never synced).
    """
    cur = conn.cursor()
    cur.execute(
        """SELECT EXTRACT(EPOCH FROM (now() - started_at)) / 3600.0
           FROM wearable_sync_log
           WHERE profile_id = %s AND status = 'success'
           ORDER BY started_at DESC
           LIMIT 1""",
        (str(profile_id),),
    )
    row = cur.fetchone()
    return float(row[0]) if row else None


def _profile_window(conn, profile_id: str, default_hours: int, max_widen: int) -> int:
    """Compute the sync window for a single profile.

    max(default_hours, ceil(hours_since_last_sync) + BUFFER), capped at max_widen.
    A profile that has never synced gets max_widen (safe initial window).
    """
    hours_ago = _last_sync_hours_ago(conn, profile_id)
    if hours_ago is None:
        return max_widen
    gap = math.ceil(hours_ago) + _BUFFER_HOURS
    return min(max(default_hours, gap), max_widen)


# ---------------------------------------------------------------------------
# Token-holding profile list
# ---------------------------------------------------------------------------

def _token_profiles(conn) -> list[str]:
    """Profile UUIDs that have a stored WHOOP token (the syncable set)."""
    cur = conn.cursor()
    cur.execute("SELECT profile_id FROM whoop_tokens ORDER BY profile_id")
    return [str(r[0]) for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# Main reconcile entry point
# ---------------------------------------------------------------------------

def reconcile(
    conn=None,
    *,
    profiles: list[str] | None = None,
    default_hours: int | None = None,
    max_widen_hours: int | None = None,
) -> dict:
    """Per-profile age-aware WHOOP reconcile.

    For each profile, computes an age-aware window, then calls
    ingest.whoop_sync.refresh_recent to pull the window from the WHOOP API.

    Args:
        conn: optional open psycopg2 connection (opened + closed internally if None).
        profiles: optional list of profile UUIDs; defaults to all token-holding profiles.
        default_hours: override push.reconcile_default_hours from system_config.
        max_widen_hours: override push.reconcile_max_widen_hours from system_config.

    Returns:
        {
          profile_id: {
            "window_hours": int,           # window used for this profile
            "results": list | dict | None, # refresh_recent output (or error dict)
          }
        }
    """
    own = conn is None
    if conn is None:
        conn = get_conn()
    try:
        cfg_default = _get_config_int(
            conn, "push.reconcile_default_hours", _FALLBACK_DEFAULT_HOURS
        )
        cfg_max = _get_config_int(
            conn, "push.reconcile_max_widen_hours", _FALLBACK_MAX_WIDEN
        )
        effective_default = default_hours if default_hours is not None else cfg_default
        effective_max = max_widen_hours if max_widen_hours is not None else cfg_max

        if profiles is None:
            profiles = _token_profiles(conn)

        out: dict = {}
        for pid in profiles:
            window = _profile_window(conn, pid, effective_default, effective_max)
            result = refresh_recent(conn, hours=window, profiles=[pid])
            out[pid] = {"window_hours": window, "results": result.get(pid)}

        return out
    finally:
        if own:
            conn.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Age-aware WHOOP reconcile — pulls missed data for each profile."
    )
    parser.add_argument(
        "--profile", default=None,
        help="Profile display_name or UUID (default: all token-holding profiles).",
    )
    parser.add_argument(
        "--hours", type=int, default=None,
        help="Override default window in hours (else reads push.reconcile_default_hours).",
    )
    args = parser.parse_args()

    conn = get_conn()
    try:
        profiles = None
        if args.profile:
            profiles = [resolve_profile(conn, args.profile)]

        results = reconcile(conn, profiles=profiles, default_hours=args.hours)
    finally:
        conn.close()

    for pid, info in results.items():
        print(f"profile={pid}  window={info['window_hours']}h  results={info['results']}")


if __name__ == "__main__":
    main()
