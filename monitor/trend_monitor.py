"""monitor/trend_monitor.py — proactive, session-start trend detection (V3-4).

Surfaces NOTABLE shifts the moment the skill opens (no cron): recovery/HRV decline,
strap non-compliance, out-of-range biomarkers, and goal progress. Runs for ALL profiles
(it's about their OWN health — not maintainer machinery). May persist findings to
trend_alerts (alert_type ∈ threshold|trend|anomaly|goal, severity ∈ info|warning|critical).

Signals are SELF-CALIBRATING where possible (no buried clinical constants, CLAUDE.md #1):
  * recovery/HRV: recent-window mean vs prior-window mean, flagged only when the drop
    exceeds the prior window's OWN standard deviation (statistical, not a fixed number).
  * abnormal biomarkers: uses each marker's min_value/max_value FROM THE DB (abnormal_labs).
  * strap compliance / goal cutoffs: relative fractions, exposed as params (tunable by
    the caller from context/system_config), not magic numbers hidden in logic.
"""
from __future__ import annotations

import psycopg2.extras

_SEVERITY_RANK = {"critical": 0, "warning": 1, "info": 2}


def _finding(alert_type, severity, title, message, data=None):
    return {"alert_type": alert_type, "severity": severity, "title": title,
            "message": message, "data": data or {}}


def recovery_hrv_shifts(conn, profile_id, *, recent_days=7, prior_days=30):
    """Flag a recovery/HRV decline when the recent mean falls more than the PRIOR
    window's own stddev below the prior mean (self-calibrating to the person)."""
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        """
        WITH scored AS (
          SELECT cycle_start::date AS d, recovery_score_pct r, hrv_ms h,
                 CASE WHEN cycle_start >= CURRENT_DATE - (%(recent)s||' days')::interval
                      THEN 'recent' ELSE 'prior' END AS win
          FROM whoop_cycles
          WHERE profile_id=%(pid)s AND recovery_score_pct IS NOT NULL
            AND cycle_start >= CURRENT_DATE - (%(prior)s||' days')::interval
        )
        SELECT win,
               round(avg(r),1) ar, round(stddev_samp(r),1) sr, count(*) n,
               round(avg(h),1) ah, round(stddev_samp(h),1) sh
        FROM scored GROUP BY win
        """,
        {"pid": profile_id, "recent": recent_days, "prior": prior_days},
    )
    w = {r["win"]: r for r in cur.fetchall()}
    out = []
    rec, pri = w.get("recent"), w.get("prior")
    if not rec or not pri or pri["n"] < 3 or rec["n"] < 2:
        return out  # not enough scored history to judge a shift
    for label, a, s, unit in (("recovery", "ar", "sr", "%"), ("HRV", "ah", "sh", " ms")):
        prior_mean, prior_sd, recent_mean = pri[a], pri[s], rec[a]
        if prior_mean is None or recent_mean is None:
            continue
        drop = float(prior_mean) - float(recent_mean)
        sd = float(prior_sd or 0)
        if sd > 0 and drop > sd:
            out.append(_finding(
                "trend", "warning",
                f"{label} trending down",
                f"{label} averaged {recent_mean}{unit} over the last {recent_days}d vs "
                f"{prior_mean}{unit} over the prior {prior_days}d "
                f"(a {round(drop,1)}{unit} drop, beyond the prior window's {prior_sd}{unit} spread).",
                {"recent": float(recent_mean), "prior": float(prior_mean), "drop": round(drop, 1)},
            ))
    return out


def strap_compliance(conn, profile_id, *, window_days=14, flag_fraction=1/3):
    """Flag when more than ``flag_fraction`` of recent cycles have no sleep/recovery
    (strap likely off) — surfaced because it makes other trends unreadable."""
    cur = conn.cursor()
    cur.execute(
        """SELECT count(*) total, count(*) FILTER (WHERE recovery_score_pct IS NULL) no_sleep
           FROM whoop_cycles
           WHERE profile_id=%s AND cycle_start >= CURRENT_DATE - (%s||' days')::interval""",
        (profile_id, window_days),
    )
    total, no_sleep = cur.fetchone()
    if not total or total < 5:
        return []
    frac = no_sleep / total
    if frac > flag_fraction:
        return [_finding(
            "anomaly", "info", "Strap compliance low",
            f"{no_sleep} of the last {total} cycles had no sleep/recovery score "
            f"({round(frac*100)}%) — recovery trends may be unreliable.",
            {"no_sleep": no_sleep, "total": total},
        )]
    return []


def abnormal_biomarkers(conn, profile_id):
    """Latest out-of-range biomarkers (uses the DB's own reference ranges via abnormal_labs)."""
    from lib.views import run_view
    res = run_view(conn, "abnormal_labs", profile_id)
    out = []
    for r in res["rows"]:
        out.append(_finding(
            "threshold", "warning",
            f"{r['marker']} {r.get('flag','')}".strip(),
            f"{r['marker']} is {r.get('qualifier') or ''}{r['value']} {r.get('unit') or ''} "
            f"({r.get('flag')} vs reference {r.get('min_value')}–{r.get('max_value')}), "
            f"as of {r.get('measured_at')}.",
            {"marker": r["marker"], "value": float(r["value"]) if r["value"] is not None else None,
             "flag": r.get("flag")},
        ))
    return out


def goal_findings(conn, profile_id):
    """Goals that are behind (computed progress < 50%) — surfaced to refocus."""
    from plan.goals import track_goals
    out = []
    for g in track_goals(conn, profile_id):
        if g.get("progress_pct") is not None and g["progress_pct"] < 50:
            out.append(_finding(
                "goal", "info", f"Goal behind: {g['title']}",
                f"{g['title']} is at {g['progress_pct']}% "
                f"(current {g.get('current_value')} vs target {g.get('target_value')}).",
                {"goal_id": g["id"], "progress_pct": g["progress_pct"]},
            ))
    return out


def _already_open(cur, profile_id, alert_type, title):
    cur.execute(
        """SELECT 1 FROM trend_alerts
           WHERE profile_id=%s AND alert_type=%s AND title=%s AND is_read=false LIMIT 1""",
        (profile_id, alert_type, title),
    )
    return cur.fetchone() is not None


def check(conn, profile_id, *, persist=False) -> list[dict]:
    """Run all monitors; return findings sorted by severity. If ``persist``, insert NEW
    findings into trend_alerts (skipping any identical unread alert already open)."""
    # The monitors run raw SQL via psycopg2; in supabase_client (App) mode `conn` is a
    # Supabase Client with no .cursor(). Trend check is a best-effort session-start step —
    # degrade silently rather than crash the session. (Full analytics need direct_role.)
    if not hasattr(conn, "cursor"):
        return []
    findings = []
    for fn in (recovery_hrv_shifts, strap_compliance, abnormal_biomarkers, goal_findings):
        try:
            findings.extend(fn(conn, profile_id))
        except Exception:
            conn.rollback()  # one monitor failing must not sink session start
    findings.sort(key=lambda f: _SEVERITY_RANK.get(f["severity"], 9))

    if persist:
        import json, uuid
        cur = conn.cursor()
        for f in findings:
            if _already_open(cur, profile_id, f["alert_type"], f["title"]):
                continue
            cur.execute(
                """INSERT INTO trend_alerts
                     (id, profile_id, alert_type, severity, title, message, data)
                   VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb)""",
                (str(uuid.uuid4()), profile_id, f["alert_type"], f["severity"],
                 f["title"], f["message"], json.dumps(f["data"])),
            )
    return findings
