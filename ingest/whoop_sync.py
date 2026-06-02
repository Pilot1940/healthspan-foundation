"""ingest/whoop_sync.py — pull WHOOP API data and upsert into whoop_* tables.

Uses the refresh token stored in .env (from whoop_oauth.py) to obtain an access
token, then paginates /v2/activity/workout, /v2/cycle, /v2/recovery, and
/v2/activity/sleep. Maps fields to live column names (as confirmed from
information_schema) and upserts via lib/contract.write().

Conflict keys (match live unique indexes):
  whoop_workouts  ON CONFLICT (profile_id, whoop_id)
  whoop_cycles    ON CONFLICT (profile_id, cycle_start)
  whoop_sleeps    ON CONFLICT (profile_id, cycle_start, sleep_onset)

Each table gets its own wearable_sync_log run row for clear reporting.

Usage (from repo root):
  python -m ingest.whoop_sync --since 2026-05-26   # last 7 days
  python -m ingest.whoop_sync --backfill            # full history from 2020-01-01
  python -m ingest.whoop_sync --since 2026-01-01   # custom window

This is a local one-off — do NOT schedule, do NOT deploy.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from lib.contract import close_sync_log, log_error, open_sync_log, write
from lib.db import get_conn, resolve_profile

_API_BASE = "https://api.prod.whoop.com/developer"
_TOKEN_URL = "https://api.prod.whoop.com/oauth/oauth2/token"
_BACKFILL_START = "2020-01-01T00:00:00Z"
_PAGE_LIMIT = 25

# ---------------------------------------------------------------------------
# Sport ID → human name (WHOOP v2 catalogue)
# ---------------------------------------------------------------------------
_SPORT_NAMES: dict[int, str] = {
    -1: "Activity", 0: "Running", 1: "Cycling", 16: "Baseball",
    17: "Basketball", 18: "Rowing", 19: "Fencing", 20: "Field Hockey",
    21: "Football", 22: "Golf", 24: "Ice Hockey", 25: "Lacrosse",
    27: "Rugby", 28: "Sailing", 29: "Skiing", 30: "Soccer",
    31: "Softball", 32: "Squash", 33: "Swimming", 34: "Tennis",
    35: "Track & Field", 36: "Volleyball", 37: "Water Polo", 38: "Wrestling",
    39: "Boxing", 42: "Dance", 43: "Pilates", 44: "Yoga", 45: "Weightlifting",
    47: "Cross Country Skiing", 48: "Functional Fitness", 49: "Duathlon",
    51: "Gymnastics", 52: "Hiking/Rucking", 53: "Horseback Riding",
    55: "Kayaking", 56: "Martial Arts", 57: "Mountain Biking",
    58: "Powerlifting", 59: "Rock Climbing", 60: "Paddleboarding",
    61: "Triathlon", 62: "Walking", 63: "Surfing", 64: "Elliptical",
    65: "Stairmaster", 67: "Meditation", 68: "Other", 71: "Cycling",
    73: "Running", 74: "Street Cycling", 75: "Obstacle Course Racing",
    82: "Skateboarding", 83: "Snowboarding", 84: "Tennis",
    87: "Spikeball", 88: "Wheelchair Pushing", 90: "Barre",
    93: "Parkour", 96: "Circus Arts", 101: "Massage Therapy",
    103: "Assault Bike", 104: "Kickboxing", 105: "Stretching", 230: "HIIT",
}


# ---------------------------------------------------------------------------
# .env reader (matches whoop_oauth approach — no external deps)
# ---------------------------------------------------------------------------

def _load_env() -> dict:
    envp = ROOT / ".env"
    env: dict = {}
    if not envp.is_file():
        return env
    for line in envp.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip()
    return env


def _write_env_key(key: str, value: str) -> None:
    envp = ROOT / ".env"
    lines = envp.read_text().splitlines(keepends=True)
    found = False
    with envp.open("w") as f:
        for line in lines:
            if line.startswith(f"{key}="):
                f.write(f"{key}={value}\n")
                found = True
            else:
                f.write(line)
        if not found:
            if lines and not lines[-1].endswith("\n"):
                f.write("\n")
            f.write(f"{key}={value}\n")


# ---------------------------------------------------------------------------
# Token management
# ---------------------------------------------------------------------------

def _basic_auth_header(client_id: str, client_secret: str) -> str:
    encoded = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    return f"Basic {encoded}"


def _get_access_token(env: dict) -> tuple[str, str]:
    """Return (access_token, new_refresh_token) using stored refresh token.

    Falls back to WHOOP_ACCESS_TOKEN if no refresh token is stored (first-run
    case where WHOOP didn't return a refresh_token in the exchange response).
    """
    client_id = env.get("WHOOP_CLIENT_ID") or os.environ.get("WHOOP_CLIENT_ID", "")
    client_secret = env.get("WHOOP_CLIENT_SECRET") or os.environ.get("WHOOP_CLIENT_SECRET", "")
    refresh_token = env.get("WHOOP_REFRESH_TOKEN") or os.environ.get("WHOOP_REFRESH_TOKEN", "")

    # Direct access token fallback (no refresh token stored yet)
    if not refresh_token:
        access_token = env.get("WHOOP_ACCESS_TOKEN") or os.environ.get("WHOOP_ACCESS_TOKEN", "")
        if access_token:
            print("  Using WHOOP_ACCESS_TOKEN directly (no refresh token stored).")
            return access_token, ""
        raise RuntimeError(
            "Neither WHOOP_REFRESH_TOKEN nor WHOOP_ACCESS_TOKEN found in .env. "
            "Run `python -m ingest.whoop_oauth` first."
        )
    if not client_id or not client_secret:
        raise RuntimeError("WHOOP_CLIENT_ID / WHOOP_CLIENT_SECRET not set in .env.")

    body = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
        "client_secret": client_secret,
    }).encode()
    req = urllib.request.Request(_TOKEN_URL, data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    req.add_header("User-Agent", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        err_body = e.read().decode(errors="replace")
        raise RuntimeError(f"WHOOP token refresh HTTP {e.code}: {err_body}") from e
    return data["access_token"], data["refresh_token"]


# ---------------------------------------------------------------------------
# API pagination helper
# ---------------------------------------------------------------------------

_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"


def _paginate(access_token: str, path: str, since_iso: str, to_iso: str) -> list:
    """Fetch all pages from a WHOOP list endpoint. Returns flat list of records."""
    headers = {
        "Authorization": f"Bearer {access_token}",
        "User-Agent": _UA,
        "Accept": "application/json",
    }
    records: list = []
    next_token: str | None = None

    while True:
        params: dict = {
            "limit": str(_PAGE_LIMIT),
            "start": since_iso,
            "end": to_iso,
        }
        if next_token:
            params["nextToken"] = next_token

        url = f"{_API_BASE}{path}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                page = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            raise RuntimeError(f"WHOOP API {path} returned HTTP {e.code}: {body}") from e

        batch = page.get("records", [])
        records.extend(batch)
        next_token = page.get("next_token")
        if not next_token or not batch:
            break

    return records


# ---------------------------------------------------------------------------
# Unit converters
# ---------------------------------------------------------------------------

def _ms_to_min(ms) -> float | None:
    if ms is None:
        return None
    return round(ms / 60_000, 2)


def _ms_to_sec(ms) -> int | None:
    if ms is None:
        return None
    return round(ms / 1000)


def _kj_to_kcal(kj) -> int | None:
    if kj is None:
        return None
    return round(kj / 4.184)


def _duration_min(start_iso: str | None, end_iso: str | None) -> float | None:
    if not start_iso or not end_iso:
        return None
    s = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
    e = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
    return round((e - s).total_seconds() / 60, 2)


# ---------------------------------------------------------------------------
# Row mappers
# ---------------------------------------------------------------------------

def _map_workout(rec: dict, profile_id: str) -> dict:
    score = rec.get("score") or {}
    zones = score.get("zone_durations") or {}  # API field is plural
    sport_id = rec.get("sport_id", -1)

    z0 = zones.get("zone_zero_milli") or 0
    z1 = zones.get("zone_one_milli") or 0
    z2 = zones.get("zone_two_milli") or 0
    z3 = zones.get("zone_three_milli") or 0
    z4 = zones.get("zone_four_milli") or 0
    z5 = zones.get("zone_five_milli") or 0
    total = z0 + z1 + z2 + z3 + z4 + z5

    def pct(ms):
        return round(ms / total * 100, 2) if total else None

    row: dict = {
        "profile_id": profile_id,
        "whoop_id": rec.get("id"),
        "workout_start": rec.get("start"),
        "workout_end": rec.get("end"),
        "timezone": rec.get("timezone_offset"),
        "activity_name": rec.get("sport_name") or _SPORT_NAMES.get(sport_id, "Activity"),
        "activity_strain": score.get("strain"),
        "avg_hr_bpm": score.get("average_heart_rate"),
        "max_hr_bpm": score.get("max_heart_rate"),
        "energy_burned_cal": _kj_to_kcal(score.get("kilojoule")),
        "duration_min": _duration_min(rec.get("start"), rec.get("end")),
        # Only include zone fields if the API returned real data (any zone > 0).
        # All-zero means zone_duration was absent — don't overwrite screenshot data.
        **({"hr_zone0_sec": _ms_to_sec(z0), "hr_zone1_sec": _ms_to_sec(z1),
             "hr_zone2_sec": _ms_to_sec(z2), "hr_zone3_sec": _ms_to_sec(z3),
             "hr_zone4_sec": _ms_to_sec(z4), "hr_zone5_sec": _ms_to_sec(z5),
             "hr_zone0_pct": pct(z0), "hr_zone1_pct": pct(z1),
             "hr_zone2_pct": pct(z2), "hr_zone3_pct": pct(z3),
             "hr_zone4_pct": pct(z4), "hr_zone5_pct": pct(z5),
            } if total > 0 else {}),
        # cardio_load_pct / muscular_load_pct not exposed by WHOOP v2 API
        "source_file": "whoop_api",
    }
    return {k: v for k, v in row.items() if v is not None}


def _map_cycle(cycle: dict, recovery: dict | None, sleep: dict | None,
               profile_id: str) -> dict:
    c_score = cycle.get("score") or {}
    r_score = (recovery or {}).get("score") or {}
    s_score = (sleep or {}).get("score") or {}
    stages = s_score.get("stage_summary") or {}
    needed = s_score.get("sleep_needed") or {}

    asleep_ms = (
        (stages.get("total_light_sleep_time_milli") or 0)
        + (stages.get("total_slow_wave_sleep_time_milli") or 0)
        + (stages.get("total_rem_sleep_time_milli") or 0)
    )
    need_ms = (
        (needed.get("baseline_milli") or 0)
        + (needed.get("need_from_sleep_debt_milli") or 0)
        + (needed.get("need_from_recent_strain_milli") or 0)
        + (needed.get("need_from_recent_nap_milli") or 0)
    )

    row: dict = {
        "profile_id": profile_id,
        "cycle_start": cycle.get("start"),
        "cycle_end": cycle.get("end"),
        "timezone": cycle.get("timezone_offset"),
        # strain / energy from cycle
        "day_strain": c_score.get("strain"),
        "energy_burned_cal": _kj_to_kcal(c_score.get("kilojoule")),
        "avg_hr_bpm": c_score.get("average_heart_rate"),
        "max_hr_bpm": c_score.get("max_heart_rate"),
        # recovery
        "recovery_score_pct": r_score.get("recovery_score"),
        "resting_hr_bpm": r_score.get("resting_heart_rate"),
        "hrv_ms": r_score.get("hrv_rmssd_milli"),
        "blood_oxygen_pct": r_score.get("spo2_percentage"),
        "skin_temp_celsius": r_score.get("skin_temp_celsius"),
        # sleep summary on the cycle row
        "sleep_onset": sleep.get("start") if sleep else None,
        "wake_onset": sleep.get("end") if sleep else None,
        "sleep_performance_pct": s_score.get("sleep_performance_percentage"),
        "sleep_consistency_pct": s_score.get("sleep_consistency_percentage"),
        "sleep_efficiency_pct": s_score.get("sleep_efficiency_percentage"),
        "respiratory_rate_rpm": s_score.get("respiratory_rate"),
        "asleep_duration_min": _ms_to_min(asleep_ms) if asleep_ms else None,
        "in_bed_duration_min": _ms_to_min(stages.get("total_in_bed_time_milli")),
        "light_sleep_min": _ms_to_min(stages.get("total_light_sleep_time_milli")),
        "deep_sws_min": _ms_to_min(stages.get("total_slow_wave_sleep_time_milli")),
        "rem_min": _ms_to_min(stages.get("total_rem_sleep_time_milli")),
        "awake_min": _ms_to_min(stages.get("total_awake_time_milli")),
        "sleep_need_min": _ms_to_min(need_ms) if need_ms else None,
        "sleep_debt_min": _ms_to_min(needed.get("need_from_sleep_debt_milli")),
        "source_file": "whoop_api",
    }
    return {k: v for k, v in row.items() if v is not None}


def _map_sleep(sleep: dict, cycle_start: str | None, profile_id: str) -> dict:
    s_score = sleep.get("score") or {}
    stages = s_score.get("stage_summary") or {}
    needed = s_score.get("sleep_needed") or {}

    asleep_ms = (
        (stages.get("total_light_sleep_time_milli") or 0)
        + (stages.get("total_slow_wave_sleep_time_milli") or 0)
        + (stages.get("total_rem_sleep_time_milli") or 0)
    )
    need_ms = (
        (needed.get("baseline_milli") or 0)
        + (needed.get("need_from_sleep_debt_milli") or 0)
        + (needed.get("need_from_recent_strain_milli") or 0)
        + (needed.get("need_from_recent_nap_milli") or 0)
    )

    row: dict = {
        "profile_id": profile_id,
        "cycle_start": cycle_start,          # derived from recovery → cycle.start
        "sleep_onset": sleep.get("start"),
        "wake_onset": sleep.get("end"),
        "timezone": sleep.get("timezone_offset"),
        "is_nap": sleep.get("nap", False),
        "sleep_performance_pct": s_score.get("sleep_performance_percentage"),
        "sleep_consistency_pct": s_score.get("sleep_consistency_percentage"),
        "sleep_efficiency_pct": s_score.get("sleep_efficiency_percentage"),
        "respiratory_rate_rpm": s_score.get("respiratory_rate"),
        "asleep_duration_min": _ms_to_min(asleep_ms) if asleep_ms else None,
        "in_bed_duration_min": _ms_to_min(stages.get("total_in_bed_time_milli")),
        "light_sleep_min": _ms_to_min(stages.get("total_light_sleep_time_milli")),
        "deep_sws_min": _ms_to_min(stages.get("total_slow_wave_sleep_time_milli")),
        "rem_min": _ms_to_min(stages.get("total_rem_sleep_time_milli")),
        "awake_min": _ms_to_min(stages.get("total_awake_time_milli")),
        "sleep_need_min": _ms_to_min(need_ms) if need_ms else None,
        "sleep_debt_min": _ms_to_min(needed.get("need_from_sleep_debt_milli")),
        "source_file": "whoop_api",
    }
    return {k: v for k, v in row.items() if v is not None}


# ---------------------------------------------------------------------------
# Per-table sync functions
# ---------------------------------------------------------------------------

_WORKOUT_CONFLICTS = ["profile_id", "whoop_id"]
_CYCLE_CONFLICTS = ["profile_id", "cycle_start"]
_SLEEP_CONFLICTS = ["profile_id", "cycle_start", "sleep_onset"]


def _build_cycle_lookup(cycles: list) -> list:
    """Return sorted [(cycle_start_iso, cycle_end_iso), ...] for workout matching."""
    result = []
    for c in cycles:
        s = c.get("start")
        e = c.get("end")
        if s:
            result.append((s, e))
    return sorted(result)


def _find_cycle_start(cycle_lookup: list, workout_start_iso: str) -> str | None:
    """Return the cycle_start for the cycle that contains workout_start, or None."""
    for cycle_start, cycle_end in cycle_lookup:
        if cycle_start <= workout_start_iso:
            if cycle_end is None or workout_start_iso < cycle_end:
                return cycle_start
    return None


def _run_sync_loop(conn, sync_id: int, items: list, record_fn, table: str,
                   conflicts: list, id_key: str = "id") -> dict:
    """Generic per-record upsert loop with SAVEPOINT isolation (prevents transaction abort)."""
    counters = {"in": len(items), "upserted": 0, "skipped": 0, "failed": 0}
    cur = conn.cursor()
    for i, item in enumerate(items):
        try:
            cur.execute("SAVEPOINT sp_sync_rec")
            row = record_fn(item)
            if row is None:          # mapper signalled skip (e.g. no cycle_start found)
                cur.execute("RELEASE SAVEPOINT sp_sync_rec")
                counters["skipped"] += 1
                continue
            result = write(conn, table, row, conflicts)
            cur.execute("RELEASE SAVEPOINT sp_sync_rec")
            if result in ("inserted", "updated"):
                counters["upserted"] += 1
            else:
                counters["skipped"] += 1
        except Exception as e:
            try:
                cur.execute("ROLLBACK TO SAVEPOINT sp_sync_rec")
            except Exception:
                conn.rollback()
            counters["failed"] += 1
            log_error(conn, sync_id, item.get(id_key, f"rec-{i}"),
                      "map_or_write", str(e), item)
    return counters


def sync_workouts(conn, access_token: str, profile_id: str,
                  since_iso: str, to_iso: str) -> dict:
    sync_id = open_sync_log(
        conn, "whoop", "api", profile_id=profile_id,
        sync_type="whoop_workouts", source_path=f"since={since_iso}"
    )
    records = _paginate(access_token, "/v2/activity/workout", since_iso, to_iso)
    # Fetch cycles to derive cycle_start (not in the workout API response)
    cycles = _paginate(access_token, "/v2/cycle", since_iso, to_iso)
    cycle_lookup = _build_cycle_lookup(cycles)

    def _map(rec):
        row = _map_workout(rec, profile_id)
        cycle_start = _find_cycle_start(cycle_lookup, rec.get("start", ""))
        if cycle_start:
            row["cycle_start"] = cycle_start
        else:
            return None  # skip — can't satisfy NOT NULL without a cycle
        return row

    counters = _run_sync_loop(conn, sync_id, records, _map,
                              "whoop_workouts", _WORKOUT_CONFLICTS)
    status = "success" if counters["failed"] == 0 else "partial"
    close_sync_log(conn, sync_id, status=status, records_in=counters["in"],
                   records_upserted=counters["upserted"],
                   records_skipped=counters["skipped"],
                   records_failed=counters["failed"])
    conn.commit()
    return {"table": "whoop_workouts", "sync_log_id": sync_id, **counters}


def sync_cycles(conn, access_token: str, profile_id: str,
                since_iso: str, to_iso: str) -> dict:
    """Sync cycles by fetching cycles + recoveries + sleeps and joining them."""
    sync_id = open_sync_log(
        conn, "whoop", "api", profile_id=profile_id,
        sync_type="whoop_cycles", source_path=f"since={since_iso}"
    )
    cycles = _paginate(access_token, "/v2/cycle", since_iso, to_iso)
    recoveries = _paginate(access_token, "/v2/recovery", since_iso, to_iso)
    sleeps_all = _paginate(access_token, "/v2/activity/sleep", since_iso, to_iso)

    recovery_by_cycle: dict = {r["cycle_id"]: r for r in recoveries if "cycle_id" in r}
    sleep_by_id: dict = {s["id"]: s for s in sleeps_all}

    def _map(cycle):
        cycle_id = cycle.get("id")
        recovery = recovery_by_cycle.get(cycle_id)
        sleep_id = (recovery or {}).get("sleep_id")
        sleep = sleep_by_id.get(sleep_id) if sleep_id else None
        return _map_cycle(cycle, recovery, sleep, profile_id)

    counters = _run_sync_loop(conn, sync_id, cycles, _map,
                              "whoop_cycles", _CYCLE_CONFLICTS)
    status = "success" if counters["failed"] == 0 else "partial"
    close_sync_log(conn, sync_id, status=status, records_in=counters["in"],
                   records_upserted=counters["upserted"],
                   records_skipped=counters["skipped"],
                   records_failed=counters["failed"])
    conn.commit()
    return {"table": "whoop_cycles", "sync_log_id": sync_id, **counters}


def sync_sleeps(conn, access_token: str, profile_id: str,
                since_iso: str, to_iso: str) -> dict:
    """Sync sleeps; derives cycle_start from recovery endpoint (sleep_id→cycle→start)."""
    sync_id = open_sync_log(
        conn, "whoop", "api", profile_id=profile_id,
        sync_type="whoop_sleeps", source_path=f"since={since_iso}"
    )
    sleeps = _paginate(access_token, "/v2/activity/sleep", since_iso, to_iso)
    recoveries = _paginate(access_token, "/v2/recovery", since_iso, to_iso)
    cycles = _paginate(access_token, "/v2/cycle", since_iso, to_iso)

    cycle_start_by_id: dict = {c["id"]: c.get("start") for c in cycles}
    sleep_to_cycle_start: dict = {}
    for r in recoveries:
        sid = r.get("sleep_id")
        cid = r.get("cycle_id")
        if sid and cid and cid in cycle_start_by_id:
            sleep_to_cycle_start[sid] = cycle_start_by_id[cid]

    # Build sorted cycle lookup as fallback for naps not in the recovery feed
    cycle_lookup = _build_cycle_lookup(cycles)

    def _map(sleep):
        sleep_id = sleep.get("id")
        cycle_start = sleep_to_cycle_start.get(sleep_id)
        if not cycle_start:
            # Naps aren't in the recovery feed — derive cycle_start from cycle list
            cycle_start = _find_cycle_start(cycle_lookup, sleep.get("start", ""))
        if not cycle_start:
            return None  # genuinely unresolvable, skip
        return _map_sleep(sleep, cycle_start, profile_id)

    counters = _run_sync_loop(conn, sync_id, sleeps, _map,
                              "whoop_sleeps", _SLEEP_CONFLICTS)
    status = "success" if counters["failed"] == 0 else "partial"
    close_sync_log(conn, sync_id, status=status, records_in=counters["in"],
                   records_upserted=counters["upserted"],
                   records_skipped=counters["skipped"],
                   records_failed=counters["failed"])
    conn.commit()
    return {"table": "whoop_sleeps", "sync_log_id": sync_id, **counters}


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Sync WHOOP API data to HealthSpan DB.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--since", metavar="DATE",
        help="ISO date (YYYY-MM-DD) to sync from. Defaults to 7 days ago.",
    )
    group.add_argument(
        "--backfill", action="store_true",
        help="Sync full history from 2020-01-01.",
    )
    parser.add_argument(
        "--profile", default="PC",
        help="Profile display_name or UUID (default: PC).",
    )
    args = parser.parse_args()

    env = _load_env()

    # Determine window
    now_utc = datetime.now(timezone.utc)
    to_iso = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    if args.backfill:
        since_iso = _BACKFILL_START
    elif args.since:
        since_iso = f"{args.since}T00:00:00Z"
    else:
        since_iso = (now_utc - timedelta(days=7)).strftime("%Y-%m-%dT00:00:00Z")

    print(f"Sync window: {since_iso} → {to_iso}")

    # Auth
    print("Refreshing WHOOP access token …")
    access_token, new_refresh = _get_access_token(env)
    if new_refresh and new_refresh != env.get("WHOOP_REFRESH_TOKEN"):
        _write_env_key("WHOOP_REFRESH_TOKEN", new_refresh)
        print("  Refresh token rotated — .env updated.")

    # Connect
    conn = get_conn()
    profile_id = resolve_profile(conn, args.profile)
    print(f"Profile: {args.profile} → {profile_id}")

    # Run syncs
    results = []
    for fn in (sync_workouts, sync_cycles, sync_sleeps):
        r = fn(conn, access_token, profile_id, since_iso, to_iso)
        results.append(r)

    conn.close()

    # Report
    print("\n── wearable_sync_log summary ──────────────────────")
    print(f"{'table':<22} {'log_id':>6}  {'in':>5}  {'upserted':>8}  {'skipped':>7}  {'failed':>6}")
    print("─" * 65)
    for r in results:
        print(
            f"{r['table']:<22} {r['sync_log_id']:>6}  {r['in']:>5}  "
            f"{r['upserted']:>8}  {r['skipped']:>7}  {r['failed']:>6}"
        )

    failures = sum(r["failed"] for r in results)
    if failures:
        print(f"\n⚠  {failures} record(s) failed — see wearable_sync_errors for detail.")
    else:
        print("\nAll records processed cleanly.")


if __name__ == "__main__":
    main()
