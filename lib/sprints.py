"""lib/sprints.py — training-sprint plan + adherence from `sprints.goals` (jsonb).

`sprints.goals` is now an OBJECT (v2):
    { "block_goals":   [str],
      "weekly_plan":   { "<weekday lowercase>": { "sessions": [str], "intensity": str,
                                                  "hard"?: bool, "recovery"?: bool } },
      "rules":         [str],
      "adherence_log": { "<YYYY-MM-DD>": { "gym": bool, "beach": bool, "pool": bool,
                                           "hike": bool, "massage": bool } } }

LEGACY: `goals` used to be a flat `[str]`. `normalize_goals()` maps that to
`{block_goals: [...], weekly_plan:{}, rules:[], adherence_log:{}}` so both shapes read safely
(historical sprint rows are still arrays).

Pure logic — no DB. The brief (`monitor/brief.py`) supplies the active sprint row + WHOOP
recovery; `mark_done()` is the only DB-touching helper (read-modify-write on the jsonb).
"""
from __future__ import annotations

from datetime import date

# The five adherence activities tracked per day (order = display order).
ACTIVITIES = ["gym", "beach", "pool", "hike", "massage"]

# WHOOP-standard recovery bands (defaults; overridable from system_config so no hardcoded
# threshold lives in logic — see CLAUDE.md rule #1). The autoregulation directives mirror
# the human-readable rule in goals.rules.
DEFAULT_GREEN_MIN = 67.0
DEFAULT_YELLOW_MIN = 34.0


def normalize_goals(goals) -> dict:
    """Return goals as the v2 object with all four keys present, from either shape."""
    if isinstance(goals, dict):
        return {
            "block_goals":   goals.get("block_goals") or [],
            "weekly_plan":   goals.get("weekly_plan") or {},
            "rules":         goals.get("rules") or [],
            "adherence_log": goals.get("adherence_log") or {},
        }
    if isinstance(goals, list):  # legacy flat array of goal strings
        return {"block_goals": goals, "weekly_plan": {}, "rules": [], "adherence_log": {}}
    return {"block_goals": [], "weekly_plan": {}, "rules": [], "adherence_log": {}}


def weekday_name(today_iso: str) -> str:
    """Lowercase weekday name for a YYYY-MM-DD string (matches weekly_plan keys)."""
    return date.fromisoformat(today_iso).strftime("%A").lower()


def todays_plan(goals_norm: dict, weekday: str) -> dict:
    """The weekly_plan entry for `weekday` ({} if none / rest day)."""
    return goals_norm.get("weekly_plan", {}).get(weekday) or {}


def autoreg(recovery_pct, green_min: float = DEFAULT_GREEN_MIN,
            yellow_min: float = DEFAULT_YELLOW_MIN) -> tuple[str, str, str]:
    """Map a WHOOP recovery % to (band, emoji, directive). recovery_pct None → unknown."""
    if recovery_pct is None:
        return ("unknown", "", "no recovery score yet — judge by feel")
    r = float(recovery_pct)
    if r >= green_min:
        return ("green", "🟢", "proceed as planned")
    if r >= yellow_min:
        return ("yellow", "🟡", "downgrade hard → moderate")
    return ("red", "🔴", "pool + beach + massage only")


def _adherence_line(goals_norm: dict, today_iso: str) -> str:
    """✓/✗ per activity for today, or '' if nothing logged for the day yet."""
    log = goals_norm.get("adherence_log", {}).get(today_iso)
    if not isinstance(log, dict):
        return ""
    marks = [f"{'✅' if log.get(a) else '⬜'} {a}" for a in ACTIVITIES]
    return "Done today: " + " ".join(marks)


def render_training_section(sprint: dict | None, today_iso: str, recovery_pct,
                            *, green_min: float = DEFAULT_GREEN_MIN,
                            yellow_min: float = DEFAULT_YELLOW_MIN) -> str:
    """The brief's '🏋️ Training' block, or '' when there is no active sprint.

    Lines: today's sessions + intensity (hard/recovery flagged), the WHOOP-autoregulated
    directive, and today's adherence ticks. Minor-safe (sessions/intensity only — no
    deficit or restriction language).
    """
    if not sprint:
        return ""
    goals = normalize_goals(sprint.get("goals"))
    weekday = weekday_name(today_iso)
    plan = todays_plan(goals, weekday)
    name = (sprint.get("name") or "Training").strip()
    wd_title = weekday.capitalize()

    lines = [f"🏋️ Training — {name}"]
    if plan:
        sessions = plan.get("sessions") or []
        intensity = (plan.get("intensity") or "").strip()
        tag = " · HARD day" if plan.get("hard") else (" · recovery day" if plan.get("recovery") else "")
        sess_txt = "; ".join(sessions) if sessions else "see plan"
        intensity_txt = f" — {intensity} intensity" if intensity else ""
        lines.append(f"Today ({wd_title}): {sess_txt}{intensity_txt}{tag}")
    else:
        lines.append(f"Today ({wd_title}): rest / unplanned")

    band, emoji, directive = autoreg(recovery_pct, green_min, yellow_min)
    if band != "unknown":
        lines.append(f"Autoregulate {emoji} {int(float(recovery_pct))}% recovery → {directive}")
    else:
        lines.append(f"Autoregulate: {directive}")

    adherence = _adherence_line(goals, today_iso)
    if adherence:
        lines.append(adherence)
    return "\n".join(lines)


def mark_done(db, sprint_id: str, date_iso: str, activity: str, *, current_goals=None) -> dict:
    """Persist a Telegram tick: set goals.adherence_log[date_iso][activity] = true.

    Read-modify-write on the jsonb (no DDL; sprints has UPDATE + has_profile_access RLS, so
    a user only writes their own sprint). Driver-agnostic: DbRest (`.update`/`.select`) or a
    psycopg2 connection (`jsonb_set`). Returns the updated adherence entry for the day.
    """
    if activity not in ACTIVITIES:
        raise ValueError(f"activity must be one of {ACTIVITIES}, got {activity!r}")

    # psycopg2 — atomic jsonb_set (preferred when available).
    if hasattr(db, "cursor") and not hasattr(db, "_base"):
        cur = db.cursor()
        cur.execute(
            """UPDATE public.sprints
               SET goals = jsonb_set(
                     goals, %s,
                     COALESCE(goals #> %s, '{}'::jsonb) || jsonb_build_object(%s, true), true)
               WHERE id = %s
               RETURNING goals #> %s""",
            ([f"adherence_log", date_iso], ["adherence_log", date_iso], activity,
             sprint_id, ["adherence_log", date_iso]),
        )
        row = cur.fetchone()
        db.commit()
        return (row[0] if row else {}) or {}

    # DbRest / REST — read-modify-write.
    if hasattr(db, "update") and hasattr(db, "_base"):
        goals = current_goals
        if goals is None:
            rows = db.select("sprints", select="goals", filters={"id": f"eq.{sprint_id}"}, limit=1)
            goals = (rows[0].get("goals") if rows else None)
        norm = normalize_goals(goals)
        day = dict(norm["adherence_log"].get(date_iso) or {})
        day[activity] = True
        norm["adherence_log"][date_iso] = day
        # Preserve the full object (write back the normalized goals so legacy arrays upgrade).
        db.update("sprints", {"id": f"eq.{sprint_id}"}, {"goals": norm})
        return day

    raise TypeError(f"mark_done: unsupported db handle {type(db)!r}")
