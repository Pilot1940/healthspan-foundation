"""analysis/trends.py — sleep + workout trend narratives (V3-5).

Computes the NUMBERS deterministically (CLAUDE.md #1: LLMs interpret, code calculates)
over the verified views, then templates a plain-language narrative the skill can relay or
re-voice. NULL-recovery aware: cycles with no sleep score are reported separately, never
averaged as zero.
"""
from __future__ import annotations

from statistics import mean

from lib.views import run_view


def _avg(xs):
    xs = [float(x) for x in xs if x is not None]
    return round(mean(xs), 1) if xs else None


def sleep_trend(conn, profile_id, *, days: int = 30) -> dict:
    """Sleep performance + debt over the window, recent vs prior half. Scored nights only."""
    rec = run_view(conn, "v_recovery_30d", profile_id, days=days)
    summ = rec.get("summary", {})
    debt = run_view(conn, "v_sleep_debt_30d", profile_id, days=days)["rows"]

    perf = [r["sleep_performance_pct"] for r in debt]
    asleep = [r["asleep_duration_min"] for r in debt]
    debt_min = [r["sleep_debt_min"] for r in debt]
    half = len(debt) // 2
    recent_perf, prior_perf = _avg(perf[half:]), _avg(perf[:half]) if half else None

    stats = {
        "days": days,
        "scored_nights": summ.get("scored_days"),
        "no_sleep_nights": summ.get("no_sleep_days"),
        "avg_sleep_performance_pct": _avg(perf),
        "avg_asleep_min": _avg(asleep),
        "avg_sleep_debt_min": _avg(debt_min),
        "recent_vs_prior_perf": {"recent": recent_perf, "prior": prior_perf},
    }
    bits = []
    if stats["avg_sleep_performance_pct"] is not None:
        bits.append(f"avg sleep performance {stats['avg_sleep_performance_pct']}% "
                    f"over {stats['scored_nights']} scored nights")
    if stats["avg_asleep_min"] is not None:
        h = int(stats["avg_asleep_min"] // 60); m = int(stats["avg_asleep_min"] % 60)
        bits.append(f"averaging {h}h{m:02d} asleep")
    if recent_perf is not None and prior_perf is not None:
        d = round(recent_perf - prior_perf, 1)
        bits.append(f"performance {'up' if d >= 0 else 'down'} {abs(d)} pts vs the prior half")
    if stats["no_sleep_nights"]:
        bits.append(f"{stats['no_sleep_nights']} night(s) had no sleep score (strap off — excluded from averages)")
    stats["narrative"] = ("Last %dd sleep: " % days) + ("; ".join(bits) + "." if bits else "no scored sleep data.")
    return stats


def workout_trend(conn, profile_id, *, days: int = 30) -> dict:
    """Volume + intensity over the window: workouts, high-intensity (Z4+Z5) and Zone2 minutes."""
    rows = run_view(conn, "v_workout_zone_summary", profile_id, days=days)["rows"]
    hi = sum(float(r.get("hi_intensity_min") or 0) for r in rows)
    z2 = sum(float(r.get("z2_min") or 0) for r in rows)
    dur = sum(float(r.get("duration_min") or 0) for r in rows)
    weeks = max(days / 7.0, 1)
    stats = {
        "days": days,
        "workouts": len(rows),
        "total_min": round(dur, 1),
        "hi_intensity_min": round(hi, 1),
        "zone2_min": round(z2, 1),
        "hi_intensity_min_per_week": round(hi / weeks, 1),
        "zone2_min_per_week": round(z2 / weeks, 1),
    }
    if rows:
        stats["narrative"] = (
            f"Last {days}d training: {len(rows)} workouts, {round(dur)} min total — "
            f"{round(hi)} min high-intensity (Z4+Z5, ~{stats['hi_intensity_min_per_week']}/wk) "
            f"and {round(z2)} min Zone2 (~{stats['zone2_min_per_week']}/wk).")
    else:
        stats["narrative"] = f"No workouts logged in the last {days}d."
    return stats
