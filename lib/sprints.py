"""lib/sprints.py — training-sprint plan + adherence from `sprints.goals` (jsonb).

`sprints.goals` is now an OBJECT (v2):
    { "block_goals":   [str],
      "weekly_plan":   { "<weekday lowercase>": { "sessions": [str], "intensity": str,
                                                  "hard"?: bool, "recovery"?: bool } },
      "rules":         [str],
      "adherence_log": { "<YYYY-MM-DD>": { "gym": bool, "beach": bool, "pool": bool,
                                           "hike": bool, "massage": bool } },
      "daily_overrides": { "<YYYY-MM-DD>": { "sessions": [str], "intensity": str,
                                             "hard"?: bool, "recovery"?: bool } } }

LEGACY: `goals` used to be a flat `[str]`. `normalize_goals()` maps that to
`{block_goals: [...], weekly_plan:{}, rules:[], adherence_log:{}, daily_overrides:{}}` so both
shapes read safely (historical sprint rows are still arrays).

TWO write surfaces touch this one jsonb: TRACKING (Telegram ticks → `adherence_log`) and
PLANNING (the claude.ai skill → `daily_overrides`). To stop one surface's full-object write
from reverting the other's (the 2026-06-12 lost-update that dropped a 'beach' tick), BOTH
DB-touching helpers route through the field-scoped atomic RPCs in mig 066:
`mark_done()` → `sprint_set_adherence`, `set_override()` → `sprint_set_override`. Everything
else here is pure logic — the brief (`monitor/brief.py`) supplies the active sprint row + WHOOP.
"""
from __future__ import annotations

import re
from datetime import date

# The five WORKOUT adherence activities tracked per day (order = display order).
ACTIVITIES = ["gym", "beach", "pool", "hike", "massage"]

# FOOD-based daily micronutrient check-in keys (kept SEPARATE from the workout ACTIVITIES).
# Tracked via FOOD, never pills — minor-safe (no supplement_regimens / pill semantics). These
# share goals.adherence_log[date] with the workout keys but render on their own brief line.
NUTRITION = ["iron", "calcium", "vitamin_d"]
# Display labels for the food check-in (key → human label).
_NUTRITION_LABEL = {"iron": "iron", "calcium": "calcium", "vitamin_d": "vitamin D"}

# WHOOP-standard recovery bands (defaults; overridable from system_config so no hardcoded
# threshold lives in logic — see CLAUDE.md rule #1).
DEFAULT_GREEN_MIN = 67.0
DEFAULT_YELLOW_MIN = 34.0

# Fallback directives, used only when goals.rules carries no parseable autoregulation rule.
# The PREFERRED source is the sprint itself: a rule like
#   "Autoregulate by WHOOP: Green=proceed; Yellow=downgrade hard->moderate; Red=pool+beach+massage only"
# so editing the sprint changes the brief — one source of truth.
DEFAULT_DIRECTIVES = {
    "green": "proceed as planned",
    "yellow": "downgrade hard → moderate",
    "red": "pool + beach + massage only",
}
_AUTOREG_RE = re.compile(
    r"green\s*=\s*(?P<green>.+?)\s*[;,]\s*"
    r"yellow\s*=\s*(?P<yellow>.+?)\s*[;,]\s*"
    r"red\s*=\s*(?P<red>.+?)\s*$",
    re.IGNORECASE,
)


def parse_autoreg_directives(rules) -> dict:
    """Extract {green,yellow,red} directive text from a `Green=…; Yellow=…; Red=…` rule.

    Returns the parsed directives (one source of truth — the sprint's own rule), or
    DEFAULT_DIRECTIVES when no rule matches (legacy sprints / differently-worded rules).
    Missing/blank captures fall back per band, so a partial rule never blanks a directive.
    """
    for rule in (rules or []):
        m = _AUTOREG_RE.search(rule or "")
        if m:
            return {b: (m.group(b).strip() or DEFAULT_DIRECTIVES[b])
                    for b in ("green", "yellow", "red")}
    return dict(DEFAULT_DIRECTIVES)


def normalize_goals(goals) -> dict:
    """Return goals as the v2 object with all keys present, from either shape.

    `daily_overrides` (optional): {<YYYY-MM-DD>: {sessions[],intensity,hard?,recovery?}} —
    a date-specific plan that SUPERSEDES weekly_plan[weekday] for that one date.
    """
    base = {"block_goals": [], "weekly_plan": {}, "rules": [], "adherence_log": {},
            "daily_overrides": {}, "food_checkin": []}
    if isinstance(goals, dict):
        return {
            "block_goals":     goals.get("block_goals") or [],
            "weekly_plan":     goals.get("weekly_plan") or {},
            "rules":           goals.get("rules") or [],
            "adherence_log":   goals.get("adherence_log") or {},
            "daily_overrides": goals.get("daily_overrides") or {},
            # which micronutrients to render in the food check-in; [] → the NUTRITION default
            "food_checkin":    goals.get("food_checkin") or [],
        }
    if isinstance(goals, list):  # legacy flat array of goal strings
        return {**base, "block_goals": goals}
    return base


def new_sprint_goals(*, block_goals=None, weekly_plan=None, rules=None,
                     food_checkin=None) -> dict:
    """Build a fresh `sprints.goals` object that INCLUDES the food micronutrient check-in
    by default, so it persists into the next block (e.g. the school-term sprint).

    `food_checkin` defaults to NUTRITION (iron/calcium/vitamin_d). Pass [] to opt a sprint
    out, or a custom subset. adherence_log/daily_overrides start empty.
    """
    return {
        "block_goals":     list(block_goals or []),
        "weekly_plan":     dict(weekly_plan or {}),
        "rules":           list(rules or []),
        "adherence_log":   {},
        "daily_overrides": {},
        "food_checkin":    list(NUTRITION if food_checkin is None else food_checkin),
    }


def weekday_name(today_iso: str) -> str:
    """Lowercase weekday name for a YYYY-MM-DD string (matches weekly_plan keys)."""
    return date.fromisoformat(today_iso).strftime("%A").lower()


def todays_plan(goals_norm: dict, weekday: str, today_iso: str | None = None) -> dict:
    """Today's plan: a `daily_overrides[today_iso]` SUPERSEDES the `weekly_plan[weekday]`
    template for that one date; otherwise the weekday template ({} if none / rest day)."""
    if today_iso:
        override = goals_norm.get("daily_overrides", {}).get(today_iso)
        if override:
            return override
    return goals_norm.get("weekly_plan", {}).get(weekday) or {}


def autoreg(recovery_pct, green_min: float = DEFAULT_GREEN_MIN,
            yellow_min: float = DEFAULT_YELLOW_MIN,
            directives: dict | None = None) -> tuple[str, str, str]:
    """Map a WHOOP recovery % to (band, emoji, directive). recovery_pct None → unknown.

    `directives` (from `parse_autoreg_directives(goals.rules)`) supplies the band text;
    omitted → DEFAULT_DIRECTIVES.
    """
    d = directives or DEFAULT_DIRECTIVES
    if recovery_pct is None:
        return ("unknown", "", "no recovery score yet — judge by feel")
    r = float(recovery_pct)
    if r >= green_min:
        return ("green", "🟢", d.get("green", DEFAULT_DIRECTIVES["green"]))
    if r >= yellow_min:
        return ("yellow", "🟡", d.get("yellow", DEFAULT_DIRECTIVES["yellow"]))
    return ("red", "🔴", d.get("red", DEFAULT_DIRECTIVES["red"]))


def _adherence_line(goals_norm: dict, today_iso: str) -> str:
    """✓/✗ per activity for today, or '' if nothing logged for the day yet."""
    log = goals_norm.get("adherence_log", {}).get(today_iso)
    if not isinstance(log, dict):
        return ""
    marks = [f"{'✅' if log.get(a) else '⬜'} {a}" for a in ACTIVITIES]
    return "Done today: " + " ".join(marks)


def _food_checkin_line(goals_norm: dict, today_iso: str) -> str:
    """FOOD micronutrient check-in for today: ✅/⬜ per key (default ⬜ until ticked).

    Minor-safe FUELLING framing (growth, never restriction). Which keys show comes from
    `goals.food_checkin` (a per-sprint override) or the NUTRITION default. Reads the same
    `adherence_log[today]` the workout ticks use; absent keys render ⬜.
    """
    keys = [k for k in (goals_norm.get("food_checkin") or NUTRITION) if k in _NUTRITION_LABEL]
    if not keys:
        return ""
    log = goals_norm.get("adherence_log", {}).get(today_iso) or {}
    marks = [f"{'✅' if log.get(k) else '⬜'} {_NUTRITION_LABEL[k]}" for k in keys]
    return "🥗 Food check-in: " + "  ".join(marks)


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
    plan = todays_plan(goals, weekday, today_iso)   # daily_overrides supersede the weekday template
    # Mark when today's plan came from a one-off date override (not the weekly template).
    is_override = bool(goals.get("daily_overrides", {}).get(today_iso))
    wd_label = f"{weekday.capitalize()}, override" if is_override else weekday.capitalize()
    name = (sprint.get("name") or "Training").strip()

    lines = [f"🏋️ Training — {name}"]
    if plan:
        sessions = plan.get("sessions") or []
        intensity = (plan.get("intensity") or "").strip()
        tag = " · HARD day" if plan.get("hard") else (" · recovery day" if plan.get("recovery") else "")
        sess_txt = "; ".join(sessions) if sessions else "see plan"
        intensity_txt = f" — {intensity} intensity" if intensity else ""
        lines.append(f"Today ({wd_label}): {sess_txt}{intensity_txt}{tag}")
    else:
        lines.append(f"Today ({wd_label}): rest / unplanned")

    directives = parse_autoreg_directives(goals.get("rules"))
    band, emoji, directive = autoreg(recovery_pct, green_min, yellow_min, directives)
    if band != "unknown":
        lines.append(f"Autoregulate {emoji} {int(float(recovery_pct))}% recovery → {directive}")
    else:
        lines.append(f"Autoregulate: {directive}")

    adherence = _adherence_line(goals, today_iso)
    if adherence:
        lines.append(adherence)

    # FOOD micronutrient check-in — renders whenever a sprint is active (independent of the
    # day's training plan). Food-based, minor-safe; ticked over REST via mark_done(nutrition key).
    food = _food_checkin_line(goals, today_iso)
    if food:
        lines.append(food)
    return "\n".join(lines)


def update_button(today_iso: str) -> dict:
    """Single '📝 Update today' button on the brief → expands (in the telegram-webhook) to the
    two-level menu (training toggles + supplement slots). Keeps the brief uncluttered."""
    return {"inline_keyboard": [[{"text": "📝 Update today", "callback_data": f"menu:{today_iso}"}]]}


def adherence_keyboard(sprint_id: str, today_iso: str, done_map: dict | None = None) -> dict:
    """Telegram inline keyboard to tick today's activities (callback → telegram-webhook).

    callback_data = `tick:<sprint_id>:<YYYY-MM-DD>:<activity>` (≤64 bytes — uuid+date+activity
    fits). Already-done activities show ✅. Laid out 3 + 2 to stay tappable on mobile.
    """
    done = done_map or {}
    def btn(a: str) -> dict:
        return {"text": f"{'✅' if done.get(a) else '⬜'} {a}",
                "callback_data": f"tick:{sprint_id}:{today_iso}:{a}"}
    return {"inline_keyboard": [[btn(a) for a in ACTIVITIES[:3]],
                               [btn(a) for a in ACTIVITIES[3:]]]}


def _sprint_profile_id(db, sprint_id: str):
    """Resolve a sprint's owning profile_id for the atomic-writer RPCs (driver-agnostic)."""
    if hasattr(db, "cursor") and not hasattr(db, "_base"):
        cur = db.cursor()
        cur.execute("SELECT profile_id FROM public.sprints WHERE id = %s", (sprint_id,))
        row = cur.fetchone()
        return str(row[0]) if row else None
    rows = db.select("sprints", select="profile_id", filters={"id": f"eq.{sprint_id}"}, limit=1)
    return (rows[0].get("profile_id") if rows else None)


def mark_done(db, sprint_id: str, date_iso: str, activity: str, value: bool = True,
              *, profile_id: str | None = None, current_goals=None) -> dict:
    """Persist a Telegram tick: set goals.adherence_log[date_iso][activity] = `value`.

    Routes through the `sprint_set_adherence` RPC (mig 066) — an atomic, subtree-scoped
    jsonb_set so a concurrent `daily_overrides` write from the PLANNING surface can't clobber
    the tick (the 2026-06-12 lost-update). Driver-agnostic: DbRest (`.rpc`) or psycopg2
    (`SELECT`). `profile_id` is the RPC's ownership pin; resolved from the sprint when omitted.
    `current_goals` is accepted for back-compat but no longer needed (the merge is server-side).
    Returns the day's adherence map.
    """
    if activity not in ACTIVITIES and activity not in NUTRITION:
        raise ValueError(
            f"activity must be one of {ACTIVITIES + NUTRITION}, got {activity!r}")
    pid = profile_id or _sprint_profile_id(db, sprint_id)

    if hasattr(db, "cursor") and not hasattr(db, "_base"):
        cur = db.cursor()
        cur.execute("SELECT public.sprint_set_adherence(%s, %s, %s, %s, %s)",
                    (sprint_id, date_iso, activity, value, pid))
        row = cur.fetchone()
        db.commit()
        return (row[0] if row else {}) or {}

    if hasattr(db, "rpc") and hasattr(db, "_base"):
        return db.rpc("sprint_set_adherence", {
            "p_sprint_id": sprint_id, "p_date": date_iso, "p_activity": activity,
            "p_value": value, "p_profile_id": pid,
        }) or {}

    raise TypeError(f"mark_done: unsupported db handle {type(db)!r}")


def set_override(db, sprint_id: str, date_iso: str, override: dict | None,
                 *, profile_id: str | None = None) -> dict | None:
    """Set (or clear) `goals.daily_overrides[date_iso]` WITHOUT touching the rest of `goals`.

    This is the PLANNING-side writer (the claude.ai skill). It routes through the
    `sprint_set_override` RPC (mig 066): an atomic, subtree-scoped jsonb_set so a Telegram
    adherence tick (TRACKING surface, writes `goals.adherence_log` on the SAME row) landing
    between this write's read and commit cannot be clobbered — that lost-update is exactly
    what dropped a 'beach' tick on 2026-06-12.

    `override=None` (or {}) CLEARS the date's override (deletes the key). `profile_id` is the
    RPC's ownership pin; resolved from the sprint when omitted. Driver-agnostic: DbRest
    (`.rpc`) or psycopg2 (`SELECT`). Returns the stored override dict (or None when cleared).
    """
    if override is not None and not isinstance(override, dict):
        raise TypeError(f"override must be a dict or None, got {type(override)!r}")
    pid = profile_id or _sprint_profile_id(db, sprint_id)
    payload = None if not override else override  # None and {} both clear

    if hasattr(db, "cursor") and not hasattr(db, "_base"):
        import json
        cur = db.cursor()
        cur.execute("SELECT public.sprint_set_override(%s, %s, %s::jsonb, %s)",
                    (sprint_id, date_iso,
                     None if payload is None else json.dumps(payload), pid))
        row = cur.fetchone()
        db.commit()
        return (row[0] if row else None)

    if hasattr(db, "rpc") and hasattr(db, "_base"):
        return db.rpc("sprint_set_override", {
            "p_sprint_id": sprint_id, "p_date": date_iso,
            "p_override": payload, "p_profile_id": pid,
        })

    raise TypeError(f"set_override: unsupported db handle {type(db)!r}")
