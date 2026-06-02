"""analysis/interval_report.py — coaching readout for a structured workout.

Narrates zone distribution, protocol compliance, recovery quality, and
transition efficiency from EXISTING DB data. No screenshot needed.

If workout_intervals rows exist, enriches with peak-consistency and valley-
depth lines; otherwise those sections are silently omitted.

Usage:
    python -m analysis.interval_report --date 2026-06-01
    python -m analysis.interval_report --id <workout_uuid>
    python -m analysis.interval_report --date 2026-06-01 --profile PC
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lib.db import get_conn, resolve_profile


# ---------------------------------------------------------------------------
# DB queries
# ---------------------------------------------------------------------------

def _fetch_workout(conn, profile_id: str, date: str | None, wid: str | None) -> dict | None:
    cur = conn.cursor()
    if wid:
        cur.execute("""
            SELECT id, workout_start, duration_min, activity_name, activity_strain,
                   avg_hr_bpm, max_hr_bpm,
                   hr_zone0_sec, hr_zone1_sec, hr_zone2_sec,
                   hr_zone3_sec, hr_zone4_sec, hr_zone5_sec,
                   protocol, tags
            FROM whoop_workouts
            WHERE id = %s::uuid AND profile_id = %s
        """, (wid, profile_id))
    else:
        cur.execute("""
            SELECT id, workout_start, duration_min, activity_name, activity_strain,
                   avg_hr_bpm, max_hr_bpm,
                   hr_zone0_sec, hr_zone1_sec, hr_zone2_sec,
                   hr_zone3_sec, hr_zone4_sec, hr_zone5_sec,
                   protocol, tags
            FROM whoop_workouts
            WHERE workout_start::date = %s AND profile_id = %s
            ORDER BY activity_strain DESC NULLS LAST, workout_start
            LIMIT 1
        """, (date, profile_id))
    row = cur.fetchone()
    if not row:
        return None
    cols = [d[0] for d in cur.description]
    return dict(zip(cols, row))


def _fetch_zone_config(conn, profile_id: str, workout_start) -> dict | None:
    cur = conn.cursor()
    cur.execute("""
        SELECT max_hr_bpm,
               z0_max_bpm, z1_low_bpm, z1_high_bpm,
               z2_low_bpm, z2_high_bpm, z3_low_bpm, z3_high_bpm,
               z4_low_bpm, z4_high_bpm, z5_low_bpm
        FROM hr_zone_config
        WHERE profile_id = %s AND effective_date <= %s
        ORDER BY effective_date DESC LIMIT 1
    """, (profile_id, workout_start))
    row = cur.fetchone()
    if not row:
        return None
    cols = [d[0] for d in cur.description]
    return dict(zip(cols, row))


def _fetch_intervals(conn, workout_id: str) -> list[dict]:
    cur = conn.cursor()
    cur.execute("""
        SELECT interval_index, kind, start_offset_sec, duration_sec,
               peak_hr_bpm, avg_hr_bpm, min_hr_bpm,
               time_to_peak_sec, recovery_hr_bpm
        FROM workout_intervals
        WHERE workout_id = %s::uuid
        ORDER BY interval_index
    """, (workout_id,))
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fmt_mmss(seconds: int | float | None) -> str:
    if seconds is None:
        return "--:--"
    s = int(round(seconds))
    return f"{s // 60}:{s % 60:02d}"


def _pct(part, total) -> str:
    if not total:
        return "0%"
    return f"{round(part / total * 100)}%"


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

def _analyse(workout: dict, zone_cfg: dict | None, protocol: dict | None,
             intervals: list[dict]) -> str:
    """Return a markdown coaching report."""

    lines: list[str] = []

    w = workout
    name = str(w.get("activity_name") or "Workout").title()
    date_str = w["workout_start"].strftime("%d %b %Y") if hasattr(w["workout_start"], "strftime") else str(w["workout_start"])[:10]
    dur = w.get("duration_min") or 0
    strain = w.get("activity_strain")
    avg_hr = w.get("avg_hr_bpm")
    max_hr = w.get("max_hr_bpm")

    # Zone seconds
    z0 = w.get("hr_zone0_sec") or 0
    z1 = w.get("hr_zone1_sec") or 0
    z2 = w.get("hr_zone2_sec") or 0
    z3 = w.get("hr_zone3_sec") or 0
    z4 = w.get("hr_zone4_sec") or 0
    z5 = w.get("hr_zone5_sec") or 0
    total_sec = z0 + z1 + z2 + z3 + z4 + z5
    has_zones = total_sec > 0

    # Zone config
    max_hr_cfg = (zone_cfg or {}).get("max_hr_bpm") or max_hr
    z4_low = (zone_cfg or {}).get("z4_low_bpm", 158)
    z4_high = (zone_cfg or {}).get("z4_high_bpm", 168)
    z5_low = (zone_cfg or {}).get("z5_low_bpm", 169)

    # Header
    lines.append(f"# {name} — {date_str}")
    lines.append(f"**Duration:** {dur:.0f} min · **Strain:** {strain:.2f}" +
                 (f" · **Avg HR:** {avg_hr} bpm · **Max HR:** {max_hr} bpm" if avg_hr else ""))
    lines.append("")

    # Protocol target box
    if protocol:
        ptype = protocol.get("type", "").upper()
        rounds = protocol.get("rounds", "?")
        work = protocol.get("work_min", "?")
        rec = protocol.get("recovery_min", "?")
        pct_lo, pct_hi = (protocol.get("pct_max") or [90, 95]) if isinstance(protocol.get("pct_max"), list) else (90, 95)
        target_hr_lo = round(max_hr_cfg * pct_lo / 100) if max_hr_cfg else "?"
        target_hr_hi = round(max_hr_cfg * pct_hi / 100) if max_hr_cfg else "?"
        lines.append(f"**Protocol:** {ptype} — {rounds}×{work} min work / {rec} min recovery")
        lines.append(f"**Target zone:** {pct_lo}–{pct_hi}% max HR = **{target_hr_lo}–{target_hr_hi} bpm** (Z4: {z4_low}–{z4_high}, spill-over Z5: {z5_low}+)")
        lines.append("")

    if not has_zones:
        lines.append("*No zone data available for this workout.*")
        return "\n".join(lines)

    # Zone distribution table
    lines.append("## Zone distribution")
    lines.append("")
    lines.append(f"| Zone | BPM range | Time | % |")
    lines.append(f"|------|-----------|------|---|")
    if zone_cfg:
        ranges = [
            (0, f"<{zone_cfg.get('z0_max_bpm',109)+1}"),
            (1, f"{zone_cfg.get('z1_low_bpm',110)}–{zone_cfg.get('z1_high_bpm',133)}"),
            (2, f"{zone_cfg.get('z2_low_bpm',134)}–{zone_cfg.get('z2_high_bpm',145)}"),
            (3, f"{zone_cfg.get('z3_low_bpm',146)}–{zone_cfg.get('z3_high_bpm',157)}"),
            (4, f"{zone_cfg.get('z4_low_bpm',158)}–{zone_cfg.get('z4_high_bpm',168)}"),
            (5, f"{zone_cfg.get('z5_low_bpm',169)}+"),
        ]
    else:
        ranges = [(i, "—") for i in range(6)]

    for zn, bpm_range in ranges:
        sec = [z0, z1, z2, z3, z4, z5][zn]
        label = f"Z{zn}"
        lines.append(f"| {label} | {bpm_range} | {_fmt_mmss(sec)} | {_pct(sec, total_sec)} |")
    lines.append("")

    # --- Analysis sections ---

    # 1. High-intensity volume (Z4 + Z5)
    hi_sec = z4 + z5
    lines.append("## High-intensity volume (Z4 + Z5)")
    lines.append("")

    if protocol:
        rounds = protocol.get("rounds", 3)
        work_min = protocol.get("work_min", 4)
        target_hi_sec = rounds * work_min * 60
        deficit = target_hi_sec - hi_sec
        lines.append(f"**Actual:** {_fmt_mmss(hi_sec)}  "
                     f"**Target:** {rounds}×{work_min} min = {_fmt_mmss(target_hi_sec)}")
        if deficit > 60:
            lines.append(f"**{_fmt_mmss(deficit)} short of target.** Each round had approximately "
                         f"{_fmt_mmss((target_hi_sec - hi_sec) / rounds)} less high-intensity time than prescribed.")
        elif deficit < -60:
            lines.append(f"**{_fmt_mmss(-deficit)} over target** — good if time-in-zone, problematic if unintentional redlining.")
        else:
            lines.append("**On target.** Volume within 1 min of prescription.")
    else:
        lines.append(f"**Z4 + Z5 time:** {_fmt_mmss(hi_sec)} ({_pct(hi_sec, total_sec)} of session)")
    lines.append("")

    # 2. Z5-vs-Z4 split — redline flag
    lines.append("## Z5 vs Z4 split (redline check)")
    lines.append("")
    if hi_sec > 0:
        z5_share = z5 / hi_sec
        z4_share = z4 / hi_sec
        lines.append(f"**Z4 (target):** {_fmt_mmss(z4)} ({_pct(z4, hi_sec)} of hi-intensity)  "
                     f"**Z5 (above target):** {_fmt_mmss(z5)} ({_pct(z5, hi_intensity=hi_sec)})")
        if z5_share > 0.5:
            lines.append(f"")
            lines.append(f"**REDLINING.** Over 50% of your high-intensity time was in Z5 ({z5_low}+ bpm). "
                         f"The 4×4 protocol targets Z4 ({z4_low}–{z4_high} bpm) — 90–95% max. "
                         f"Z5 means you're above 95% max and spending adaptation budget faster than necessary. "
                         f"Pace to hold {z4_low}–{z4_high} and you'll recover faster between rounds.")
        elif z5_share > 0.35:
            lines.append(f"")
            lines.append(f"**Slightly hot.** {_pct(z5, hi_sec)} of hi-intensity in Z5. "
                         f"Aim to keep Z5 under 30% — pace the first minute of each interval.")
        else:
            lines.append(f"**Well-paced.** Z4 dominant. Z5 spill-over is normal at maximal effort.")
    else:
        lines.append("*No high-intensity data.*")
    lines.append("")

    # 3. Recovery quality (Z1 vs Z2)
    lines.append("## Recovery quality (between intervals)")
    lines.append("")
    rec_sec = z1 + z2
    if protocol:
        rounds = protocol.get("rounds", 3)
        rec_min = protocol.get("recovery_min", 3)
        expected_rec_sec = rounds * rec_min * 60
        expected_z1_sec = rounds * rec_min * 60 * 0.5  # aim for lower half in recovery
        lines.append(f"**Total Z1+Z2:** {_fmt_mmss(rec_sec)}  "
                     f"**Expected recovery time:** ~{_fmt_mmss(expected_rec_sec)} ({rounds}×{rec_min} min)")
    else:
        lines.append(f"**Total Z1+Z2:** {_fmt_mmss(rec_sec)}")

    if rec_sec > 0:
        z1_share = z1 / rec_sec
        lines.append(f"**Z1 (deep recovery, <{(zone_cfg or {}).get('z1_high_bpm', 133)+1} bpm):** {_fmt_mmss(z1)} ({_pct(z1, rec_sec)} of recovery time)")
        lines.append(f"**Z2 (shallow recovery, {(zone_cfg or {}).get('z2_low_bpm',134)}–{(zone_cfg or {}).get('z2_high_bpm',145)} bpm):** {_fmt_mmss(z2)} ({_pct(z2, rec_sec)} of recovery time)")
        lines.append("")
        if z1_share < 0.3:
            lines.append(f"**Shallow recovery.** Only {_pct(z1, rec_sec)} of your rest time hit Z1. "
                         f"Heart rate isn't dropping below {(zone_cfg or {}).get('z1_high_bpm', 133)} bpm between intervals — "
                         f"sitting in Z2 ({(zone_cfg or {}).get('z2_low_bpm',134)}–{(zone_cfg or {}).get('z2_high_bpm',145)}) instead. "
                         f"This signals residual fatigue. With more aerobic base, Z1 share will rise.")
        elif z1_share < 0.5:
            lines.append(f"Moderate recovery. Z1 share ({_pct(z1, rec_sec)}) is building. "
                         f"Target is ≥50% of rest time in Z1 for complete clearance.")
        else:
            lines.append(f"Good recovery depth. Majority of rest time in Z1 — cardiovascular clearance is efficient.")
    lines.append("")

    # 4. Transition time (Z3)
    lines.append("## Transition efficiency (Z3)")
    lines.append("")
    if protocol:
        rounds = protocol.get("rounds", 3)
        z3_per_round = z3 / rounds if rounds else z3
        lines.append(f"**Total Z3 time:** {_fmt_mmss(z3)} across {rounds} rounds = **{_fmt_mmss(z3_per_round)} per round**")
        target_z3_per_round = 120  # 2 min per round is efficient
        if z3_per_round > 240:
            lines.append(f"**High transition time.** {_fmt_mmss(z3_per_round)}/round in Z3 means a lot of "
                         f"time ramping up and ramping down — not at target and not recovering. "
                         f"Aim for <2 min/round. This often reflects a slow cardiac response; "
                         f"it improves with consistent Z2 base work.")
        elif z3_per_round > 120:
            lines.append(f"Moderate transitions ({_fmt_mmss(z3_per_round)}/round). "
                         f"Under 2 min/round is the target as fitness improves.")
        else:
            lines.append(f"Efficient transitions. <2 min/round in Z3 indicates fast HR response.")
    else:
        lines.append(f"**Z3 time:** {_fmt_mmss(z3)} ({_pct(z3, total_sec)} of session)")
    lines.append("")

    # 5. Interval trace enrichment (only if workout_intervals rows exist)
    work_intervals = [iv for iv in intervals if iv.get("kind") in ("work", None)]
    rec_intervals = [iv for iv in intervals if iv.get("kind") == "recovery"]

    if work_intervals:
        lines.append("## Interval detail (from HR trace)")
        lines.append("")
        lines.append(f"| Round | Peak HR | Avg HR | Time-to-peak | Recovery HR |")
        lines.append(f"|-------|---------|--------|-------------|-------------|")
        for iv in work_intervals:
            idx = (iv.get("interval_index") or 0) + 1
            peak = iv.get("peak_hr_bpm", "—")
            avg = iv.get("avg_hr_bpm", "—")
            ttp = _fmt_mmss(iv.get("time_to_peak_sec"))
            rec_hr = iv.get("recovery_hr_bpm", "—")
            lines.append(f"| {idx} | {peak} | {avg} | {ttp} | {rec_hr} |")
        lines.append("")

        # Peak consistency
        peaks = [iv["peak_hr_bpm"] for iv in work_intervals if iv.get("peak_hr_bpm")]
        if len(peaks) >= 2:
            peak_spread = max(peaks) - min(peaks)
            lines.append(f"**Peak consistency:** {min(peaks)}–{max(peaks)} bpm (spread {peak_spread} bpm). "
                         + ("Consistent — no fatigue drift." if peak_spread <= 5
                            else f"{'Moderate' if peak_spread <= 10 else 'Significant'} drift — "
                                 f"{'pacing or accumulating fatigue.' if peak_spread > 5 else ''}"))
            lines.append("")

        # Valley depth
        valleys = [iv["recovery_hr_bpm"] for iv in work_intervals if iv.get("recovery_hr_bpm")]
        if not valleys:
            valleys = [iv["min_hr_bpm"] for iv in rec_intervals if iv.get("min_hr_bpm")]
        if valleys and zone_cfg:
            z1_hi = zone_cfg.get("z1_high_bpm", 133)
            deep = [v for v in valleys if v <= z1_hi]
            lines.append(f"**Recovery valleys:** {', '.join(str(v) for v in valleys)} bpm. "
                         + (f"{len(deep)}/{len(valleys)} rounds reached Z1 (≤{z1_hi} bpm)."
                            if deep else f"No rounds reached Z1 (≤{z1_hi} bpm) — HR not clearing fully between intervals."))
            lines.append("")

    # Footer
    lines.append("---")
    if not protocol:
        lines.append("*No protocol set on this workout. Set one with:*")
        lines.append(f"```sql")
        lines.append(f"UPDATE whoop_workouts SET protocol = '{{\"type\":\"4x4\",\"pct_max\":[90,95],\"rounds\":3,\"work_min\":4,\"recovery_min\":3}}'::jsonb")
        lines.append(f"WHERE id = '{workout['id']}';")
        lines.append(f"```")

    return "\n".join(lines)


def _pct(part, hi_intensity=None, total=None) -> str:
    denom = hi_intensity if hi_intensity is not None else total
    if not denom:
        return "0%"
    return f"{round(part / denom * 100)}%"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Coaching readout for a structured workout.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--date", metavar="YYYY-MM-DD", help="Workout date (picks highest-strain workout).")
    group.add_argument("--id", metavar="UUID", help="Exact workout UUID.")
    parser.add_argument("--profile", default="PC", help="Profile name or UUID (default: PC).")
    args = parser.parse_args()

    conn = get_conn()
    profile_id = resolve_profile(conn, args.profile)

    workout = _fetch_workout(conn, profile_id, args.date, args.id)
    if not workout:
        print(f"No workout found for {args.date or args.id}.")
        sys.exit(1)

    zone_cfg = _fetch_zone_config(conn, profile_id, workout["workout_start"])
    protocol = workout.get("protocol")
    intervals = _fetch_intervals(conn, str(workout["id"]))
    conn.close()

    report = _analyse(workout, zone_cfg, protocol, intervals)
    print(report)


if __name__ == "__main__":
    main()
