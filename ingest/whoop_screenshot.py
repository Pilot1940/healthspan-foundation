"""ingest/whoop_screenshot.py — digitise the WHOOP workout HR curve (Layer B).

The API already holds zone durations / strain / calories / avg-max HR EXACTLY
(verified to the second on Jun-1). This module does NOT re-ingest those — it
exists ONLY to capture the one thing the API cannot give: the per-second HR
curve shape (the blue line) and the per-round interval structure derived from it.

What it writes (ADDITIVE ONLY — never overwrites API aggregates):
  workout_hr_samples   — digitised curve points (t_offset_sec, hr_bpm), source='screenshot'
  workout_intervals    — peaks/valleys derived from the curve, source='screenshot'
  whoop_workouts.notes — the coach note text (only if currently NULL)
  whoop_workouts.cardio_load_pct / muscular_load_pct — only if currently NULL

Matching: each screenshot is matched to a whoop_workouts row by the visible
start/end time + folder/file date (±2 min) → whoop_id. No confident match →
needs_review (never guesses).

Idempotency: every processed image is logged to wearable_sync_log
(method='screenshot', source_path=<png>). On re-run, already-succeeded paths
are skipped — no double-write of samples/intervals.

The HR-curve values are APPROXIMATE by design (curve-reading, not true
per-second telemetry) and every sample/interval row is stamped source='screenshot'.

Usage:
    python -m ingest.whoop_screenshot --scan "<folder>"
    python -m ingest.whoop_screenshot --scan "<folder>" --profile PC --model claude-sonnet-4-6
    python -m ingest.whoop_screenshot --image <path-to.png>     # single image
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lib.db import get_conn, resolve_profile
from lib.contract import close_sync_log, log_error, open_sync_log

DEFAULT_SCAN = "~/Library/CloudStorage/Dropbox/Personal/Health - Fitness/Whoop Files/Processed"
DEFAULT_MODEL = "claude-sonnet-4-6"
RAW_DATA_DIRNAME = "Whoop RAW Data"   # CSV exports — counted, never imported (API has them)
MATCH_WINDOW_SEC = 120                # ±2 min start-time match window


# ---------------------------------------------------------------------------
# .env loader (matches whoop_sync pattern)
# ---------------------------------------------------------------------------

def _load_env() -> None:
    envp = ROOT / ".env"
    if not envp.is_file():
        return
    for line in envp.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())


# ---------------------------------------------------------------------------
# Vision extraction (Claude) — the curve digitiser
# ---------------------------------------------------------------------------

_VISION_PROMPT = """You are reading a WHOOP fitness app screenshot. Return ONLY a JSON object.

First classify: is this a WORKOUT screen showing an Activity Strain heart-rate
chart (a blue HR line over time, with a zone-duration table below)?
Sleep screens, recovery screens, overview screens, and anything without an
HR-over-time workout chart are NOT workout charts.

If it is NOT a workout HR chart, return exactly: {"is_workout_chart": false}

If it IS a workout HR chart, return this JSON:
{
  "is_workout_chart": true,
  "start_local": "6:19 PM",          // the start time label under the chart, verbatim
  "end_local": "7:15 PM",            // the end time label under the chart, verbatim
  "duration_sec": 3376,              // from the "DURATION 0:56:16" label → seconds
  "avg_hr_bpm": 140,                 // for match sanity-check only
  "max_hr_bpm": 181,                 // for match sanity-check only (may be absent)
  "cardio_pct": 68,                  // CARDIO % from the top bar, if shown (else null)
  "muscular_pct": 32,                // MUSCULAR % from the top bar, if shown (else null)
  "coach_note": "Big jump in intensity today...",  // full coach-note text if shown (else null)
  "zone_table": {                    // for MATCH SANITY-CHECK ONLY — never stored
    "z5_sec": 448, "z4_sec": 275, "z3_sec": 702, "z2_sec": 1108, "z1_sec": 0, "z0_sec": 134
  },
  "hr_samples": [                    // READ THE BLUE CURVE — approximate is fine
    {"t_offset_sec": 0, "hr_bpm": 105},
    {"t_offset_sec": 30, "hr_bpm": 120}
    // sample the curve roughly every 20-40s across the whole duration,
    // capturing the peaks and valleys faithfully (the SHAPE matters most).
    // t_offset_sec is seconds from workout start (0) to end (duration_sec).
    // hr_bpm must be 30-230.
  ]
}

Read the curve's peaks and troughs carefully — the interval structure is derived
from them. Do not invent precision; approximate the visible shape. Return JSON only."""


def _extract_via_vision(image_path: Path, model: str) -> dict:
    """Send the image to Claude vision; return the parsed JSON dict.

    Raises RuntimeError on missing key / API error / unparseable response.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set in .env — cannot run vision extraction")

    import anthropic

    media_type = "image/png" if image_path.suffix.lower() == ".png" else "image/jpeg"
    img_b64 = base64.standard_b64encode(image_path.read_bytes()).decode()

    client = anthropic.Anthropic(api_key=api_key)
    resp = client.messages.create(
        model=model,
        max_tokens=4096,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {
                    "type": "base64", "media_type": media_type, "data": img_b64}},
                {"type": "text", "text": _VISION_PROMPT},
            ],
        }],
    )
    text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()
    # tolerate ```json fences
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip().rstrip("`").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"vision response not valid JSON: {text[:200]}") from e


# ---------------------------------------------------------------------------
# Interval derivation from the digitised curve
# ---------------------------------------------------------------------------

def _derive_intervals(samples: list[dict], z4_low: int) -> list[dict]:
    """Detect work bouts (excursions above z4_low) and the recovery valleys between.

    A simple, robust heuristic: a 'work' interval is a contiguous span where HR
    stays at/above z4_low (the high-intensity threshold). The valley after it is
    the minimum HR before the next work bout begins.
    """
    if not samples:
        return []
    samples = sorted(samples, key=lambda s: s["t_offset_sec"])
    intervals: list[dict] = []
    in_work = False
    cur_start = None
    cur_points: list[dict] = []
    idx = 0

    def _flush_work(end_offset):
        nonlocal idx
        if not cur_points:
            return
        peak = max(cur_points, key=lambda p: p["hr_bpm"])
        avg = round(sum(p["hr_bpm"] for p in cur_points) / len(cur_points))
        mn = min(p["hr_bpm"] for p in cur_points)
        intervals.append({
            "interval_index": idx,
            "kind": "work",
            "start_offset_sec": cur_points[0]["t_offset_sec"],
            "duration_sec": end_offset - cur_points[0]["t_offset_sec"],
            "peak_hr_bpm": peak["hr_bpm"],
            "avg_hr_bpm": avg,
            "min_hr_bpm": mn,
            "time_to_peak_sec": peak["t_offset_sec"] - cur_points[0]["t_offset_sec"],
        })
        idx += 1

    for i, s in enumerate(samples):
        hot = s["hr_bpm"] >= z4_low
        if hot and not in_work:
            in_work = True
            cur_points = [s]
        elif hot and in_work:
            cur_points.append(s)
        elif not hot and in_work:
            in_work = False
            _flush_work(s["t_offset_sec"])
            cur_points = []
    if in_work:
        _flush_work(samples[-1]["t_offset_sec"])

    # Recovery valley after each work interval = min HR between this work end and next work start
    for n, iv in enumerate(intervals):
        win_start = iv["start_offset_sec"] + iv["duration_sec"]
        win_end = intervals[n + 1]["start_offset_sec"] if n + 1 < len(intervals) else samples[-1]["t_offset_sec"]
        valley_pts = [s for s in samples if win_start <= s["t_offset_sec"] <= win_end]
        if valley_pts:
            valley = min(valley_pts, key=lambda p: p["hr_bpm"])
            iv["recovery_hr_bpm"] = valley["hr_bpm"]
            iv["recovery_drop_bpm"] = iv["peak_hr_bpm"] - valley["hr_bpm"]
    return intervals


# ---------------------------------------------------------------------------
# Matching — screenshot → whoop_workouts row
# ---------------------------------------------------------------------------

def _to_local_wallclock(ws, tz_str: str | None) -> datetime | None:
    """Render a tz-aware UTC timestamp as naive local wall-clock using a '+05:30' offset.

    WHOOP stores workout_start as UTC (timestamptz); the screenshot shows local
    wall-clock. Convert UTC → the stored offset to compare like-for-like.
    """
    if ws is None:
        return None
    from datetime import timezone as _tz
    # normalise to UTC-naive first
    if hasattr(ws, "tzinfo") and ws.tzinfo is not None:
        utc_naive = ws.astimezone(_tz.utc).replace(tzinfo=None)
    else:
        utc_naive = ws
    tz_str = (tz_str or "").strip()
    if tz_str and tz_str[0] in "+-" and ":" in tz_str:
        try:
            sign = 1 if tz_str[0] == "+" else -1
            hh, mm = tz_str[1:].split(":")
            return utc_naive + sign * timedelta(hours=int(hh), minutes=int(mm))
        except ValueError:
            pass
    return utc_naive


def _parse_local_time(start_local: str, folder_date: str) -> datetime | None:
    """Combine '6:19 PM' + '2026-06-01' → naive local datetime."""
    if not start_local or not folder_date:
        return None
    for fmt in ("%I:%M %p", "%I:%M%p", "%H:%M"):
        try:
            t = datetime.strptime(start_local.strip().upper().replace("  ", " "), fmt)
            d = datetime.strptime(folder_date, "%Y-%m-%d")
            return d.replace(hour=t.hour, minute=t.minute)
        except ValueError:
            continue
    return None


def _match_workout(conn, profile_id: str, extracted: dict, folder_date: str) -> dict | None:
    """Return the matched whoop_workouts row (id, whoop_id, duration_min, ...) or None.

    Matches on local start time (workout_start rendered in its stored tz offset)
    within ±MATCH_WINDOW_SEC, cross-checked against duration. Never guesses.
    """
    cur = conn.cursor()
    dur_sec = extracted.get("duration_sec")
    local_start = _parse_local_time(extracted.get("start_local", ""), folder_date)

    # Candidate set: workouts on the folder date ±1 day for this profile
    cur.execute("""
        SELECT id, whoop_id, workout_start, timezone, duration_min,
               cardio_load_pct, muscular_load_pct, tags
        FROM whoop_workouts
        WHERE profile_id = %s
          AND workout_start::date BETWEEN %s::date - 1 AND %s::date + 1
    """, (profile_id, folder_date, folder_date))
    cols = [d[0] for d in cur.description]
    candidates = [dict(zip(cols, r)) for r in cur.fetchall()]
    if not candidates:
        return None

    scored = []
    for c in candidates:
        score = 0
        reasons = []
        # 1. duration match (strong signal)
        if dur_sec and c["duration_min"] is not None:
            if abs(c["duration_min"] * 60 - dur_sec) <= MATCH_WINDOW_SEC:
                score += 2
                reasons.append("duration±2m")
        # 2. local start-time match — render workout_start as local wall-clock
        if local_start is not None:
            local_ws = _to_local_wallclock(c["workout_start"], c.get("timezone"))
            if local_ws is not None and abs((local_ws - local_start).total_seconds()) <= MATCH_WINDOW_SEC:
                score += 3
                reasons.append("start±2m")
        if score > 0:
            scored.append((score, reasons, c))

    if not scored:
        return None
    scored.sort(key=lambda x: x[0], reverse=True)
    best_score, reasons, best = scored[0]
    # require a confident match: start-time OR (duration AND only one candidate that day)
    if best_score >= 3 or (best_score >= 2 and len([s for s in scored if s[0] >= 2]) == 1):
        best["_match_reasons"] = reasons
        return best
    return None


# ---------------------------------------------------------------------------
# Writers (additive only)
# ---------------------------------------------------------------------------

def _write_samples(conn, profile_id: str, workout_id: str, samples: list[dict]) -> int:
    cur = conn.cursor()
    n = 0
    for s in samples:
        hr = s.get("hr_bpm")
        t = s.get("t_offset_sec")
        if hr is None or t is None or not (30 <= hr <= 230):
            continue
        cur.execute("""
            INSERT INTO workout_hr_samples (profile_id, workout_id, t_offset_sec, hr_bpm)
            VALUES (%s, %s, %s, %s)
        """, (profile_id, workout_id, int(t), int(hr)))
        n += 1
    return n


def _write_intervals(conn, profile_id: str, workout_id: str, intervals: list[dict],
                     workout_dur_sec: float | None) -> int:
    cur = conn.cursor()
    n = 0
    for iv in intervals:
        # validate: interval within workout duration
        if workout_dur_sec and iv.get("duration_sec", 0) > workout_dur_sec + MATCH_WINDOW_SEC:
            continue
        cur.execute("""
            INSERT INTO workout_intervals
              (profile_id, workout_id, interval_index, kind, start_offset_sec,
               duration_sec, peak_hr_bpm, avg_hr_bpm, min_hr_bpm, time_to_peak_sec,
               recovery_hr_bpm, recovery_drop_bpm, source)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'screenshot')
        """, (profile_id, workout_id, iv["interval_index"], iv.get("kind"),
              iv.get("start_offset_sec"), iv.get("duration_sec"), iv.get("peak_hr_bpm"),
              iv.get("avg_hr_bpm"), iv.get("min_hr_bpm"), iv.get("time_to_peak_sec"),
              iv.get("recovery_hr_bpm"), iv.get("recovery_drop_bpm")))
        n += 1
    return n


def _fill_notes_and_split(conn, workout: dict, extracted: dict) -> list[str]:
    """Capture coach note (→ tags array) + cardio/muscular ONLY where currently empty/NULL.

    There is no `notes` column on whoop_workouts; the coach note is stored as a
    tagged object appended to the `tags` JSONB array. Idempotent: skipped if a
    coach_note element is already present.
    """
    cur = conn.cursor()
    filled = []

    note = extracted.get("coach_note")
    if note:
        tags = workout.get("tags") or []
        has_note = any(isinstance(t, dict) and "coach_note" in t for t in tags)
        if not has_note:
            new_tags = list(tags) + [{"coach_note": note, "source": "screenshot"}]
            cur.execute("UPDATE whoop_workouts SET tags = %s::jsonb WHERE id = %s::uuid",
                        (json.dumps(new_tags), workout["id"]))
            filled.append("coach_note")

    cardio = extracted.get("cardio_pct")
    if cardio is not None and workout.get("cardio_load_pct") is None:
        cur.execute("UPDATE whoop_workouts SET cardio_load_pct = %s WHERE id = %s::uuid", (cardio, workout["id"]))
        filled.append("cardio_load_pct")
    musc = extracted.get("muscular_pct")
    if musc is not None and workout.get("muscular_load_pct") is None:
        cur.execute("UPDATE whoop_workouts SET muscular_load_pct = %s WHERE id = %s::uuid", (musc, workout["id"]))
        filled.append("muscular_load_pct")
    return filled


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------

def _already_done(conn, source_path: str) -> bool:
    cur = conn.cursor()
    cur.execute("""
        SELECT 1 FROM wearable_sync_log
        WHERE method = 'screenshot' AND source_path = %s AND status = 'success'
        LIMIT 1
    """, (source_path,))
    return cur.fetchone() is not None


# ---------------------------------------------------------------------------
# Folder helpers
# ---------------------------------------------------------------------------

def _folder_date(png: Path) -> str:
    """Derive YYYY-MM-DD from filename (Screenshot_YYYYMMDD-...) or parent folder name."""
    name = png.name
    # Screenshot_20260601-191654.png
    if name.startswith("Screenshot_") and len(name) > 19:
        digits = name[11:19]
        if digits.isdigit():
            return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
    # parent folder like 2026-06-01
    parent = png.parent.name
    try:
        datetime.strptime(parent, "%Y-%m-%d")
        return parent
    except ValueError:
        return ""


def _collect_pngs(scan_root: Path) -> tuple[list[Path], list[Path]]:
    """Return (png_paths, csv_paths_under_raw). Skips the Whoop RAW Data subtree for PNGs."""
    pngs, csvs = [], []
    for p in sorted(scan_root.rglob("*")):
        if RAW_DATA_DIRNAME in p.parts:
            if p.suffix.lower() == ".csv":
                csvs.append(p)
            continue
        if p.suffix.lower() == ".png":
            pngs.append(p)
    return pngs, csvs


# ---------------------------------------------------------------------------
# Per-image processing
# ---------------------------------------------------------------------------

def process_image(conn, profile_id: str, png: Path, model: str,
                  z4_low: int, summary: dict) -> None:
    source_path = str(png)
    if _already_done(conn, source_path):
        summary["skipped_done"].append(source_path)
        return

    sync_id = open_sync_log(conn, "whoop", "screenshot",
                            profile_id=profile_id, sync_type="whoop_screenshot",
                            source_path=source_path)
    conn.commit()

    try:
        extracted = _extract_via_vision(png, model)
    except Exception as e:
        close_sync_log(conn, sync_id, status="failed", records_in=0, records_failed=1)
        log_error(conn, sync_id, png.name, "vision_error", str(e), {"path": source_path})
        conn.commit()
        summary["needs_review"].append((source_path, f"vision error: {e}"))
        return

    if not extracted.get("is_workout_chart"):
        close_sync_log(conn, sync_id, status="success", records_in=0)
        conn.commit()
        summary["skipped_not_workout"].append(source_path)
        return

    summary["classified_workout"] += 1
    folder_date = _folder_date(png)
    workout = _match_workout(conn, profile_id, extracted, folder_date)
    if not workout:
        close_sync_log(conn, sync_id, status="failed", records_in=0)
        log_error(conn, sync_id, png.name, "no_match",
                  f"no confident workout match (date={folder_date}, start={extracted.get('start_local')})",
                  {"extracted_start": extracted.get("start_local"),
                   "extracted_duration_sec": extracted.get("duration_sec")})
        conn.commit()
        summary["needs_review"].append(
            (source_path, f"no match (date {folder_date}, start {extracted.get('start_local')})"))
        return

    # Write samples + intervals (additive), fill notes/split where NULL
    samples = extracted.get("hr_samples") or []
    dur_sec = workout["duration_min"] * 60 if workout.get("duration_min") else extracted.get("duration_sec")
    n_samples = _write_samples(conn, profile_id, str(workout["id"]), samples)
    intervals = _derive_intervals(samples, z4_low)
    n_intervals = _write_intervals(conn, profile_id, str(workout["id"]), intervals, dur_sec)
    filled = _fill_notes_and_split(conn, workout, extracted)

    close_sync_log(conn, sync_id, status="success",
                   records_in=len(samples), records_upserted=n_samples)
    conn.commit()
    summary["matched"] += 1
    summary["written"].append({
        "path": source_path,
        "whoop_id": workout["whoop_id"],
        "samples": n_samples,
        "intervals": n_intervals,
        "filled": filled,
        "match": workout.get("_match_reasons"),
    })


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Digitise WHOOP workout HR curves from screenshots.")
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--scan", metavar="FOLDER", help="Folder to scan recursively for PNGs.")
    g.add_argument("--image", metavar="PNG", help="Process a single PNG.")
    parser.add_argument("--profile", default="PC", help="Profile name or UUID (default PC).")
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"Claude model (default {DEFAULT_MODEL}).")
    args = parser.parse_args()

    _load_env()
    conn = get_conn()
    profile_id = resolve_profile(conn, args.profile)

    # high-intensity threshold for interval detection
    cur = conn.cursor()
    cur.execute("""SELECT z4_low_bpm FROM hr_zone_config
                   WHERE profile_id = %s ORDER BY effective_date DESC LIMIT 1""", (profile_id,))
    row = cur.fetchone()
    z4_low = row[0] if row else 158

    summary = {
        "scanned": 0, "classified_workout": 0, "matched": 0,
        "written": [], "skipped_not_workout": [], "skipped_done": [],
        "needs_review": [], "csv_skipped": [],
    }

    if args.image:
        pngs = [Path(args.image).expanduser()]
        csvs = []
    else:
        scan_root = Path(args.scan).expanduser()
        if not scan_root.is_dir():
            print(f"Scan folder not found: {scan_root}"); sys.exit(1)
        pngs, csvs = _collect_pngs(scan_root)
        summary["csv_skipped"] = [str(p) for p in csvs]

    summary["scanned"] = len(pngs)
    for png in pngs:
        process_image(conn, profile_id, png, args.model, z4_low, summary)

    conn.close()
    _print_summary(summary)


def _print_summary(s: dict) -> None:
    print("\n══ WHOOP screenshot digitisation summary ══════════════════════")
    print(f"  images scanned          : {s['scanned']}")
    print(f"  classified as workout   : {s['classified_workout']}")
    print(f"  matched to a workout    : {s['matched']}")
    print(f"  skipped (not a workout) : {len(s['skipped_not_workout'])}")
    print(f"  skipped (already done)  : {len(s['skipped_done'])}")
    print(f"  needs review            : {len(s['needs_review'])}")
    print(f"  CSV files skipped (RAW) : {len(s['csv_skipped'])}")

    if s["written"]:
        print("\n  ── written ──")
        for w in s["written"]:
            print(f"    {Path(w['path']).name}  whoop_id={(w['whoop_id'] or '')[:12]}…  "
                  f"{w['samples']} samples, {w['intervals']} intervals"
                  + (f", filled {w['filled']}" if w['filled'] else "")
                  + (f"  [{','.join(w['match'])}]" if w.get('match') else ""))

    if s["needs_review"]:
        print("\n  ── needs review ──")
        for path, reason in s["needs_review"]:
            print(f"    {Path(path).name}: {reason}")


if __name__ == "__main__":
    main()
