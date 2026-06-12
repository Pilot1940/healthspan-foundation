"""Unit tests for lib/sprints.py — training plan + adherence from sprints.goals jsonb."""
from __future__ import annotations

from unittest.mock import MagicMock

from lib import sprints

GOALS_V2 = {
    "block_goals": ["VO2 base", "stay injury-free"],
    "weekly_plan": {
        "monday": {"sessions": ["gym hard"], "intensity": "hard", "hard": True},
        "friday": {"sessions": ["pool easy", "beach"], "intensity": "moderate"},
        "sunday": {"sessions": ["massage"], "intensity": "easy", "recovery": True},
    },
    "rules": ["Autoregulate by WHOOP"],
    "adherence_log": {"2026-06-12": {"gym": True, "beach": False, "pool": True}},
}


def test_normalize_object_passthrough():
    n = sprints.normalize_goals(GOALS_V2)
    assert n["block_goals"] == ["VO2 base", "stay injury-free"]
    assert "monday" in n["weekly_plan"]


def test_normalize_legacy_array():
    n = sprints.normalize_goals(["fat loss", "zone 2"])
    assert n["block_goals"] == ["fat loss", "zone 2"]
    assert n["weekly_plan"] == {} and n["rules"] == [] and n["adherence_log"] == {}


def test_normalize_none():
    n = sprints.normalize_goals(None)
    assert n == {"block_goals": [], "weekly_plan": {}, "rules": [],
                 "adherence_log": {}, "daily_overrides": {}}


def test_weekday_name():
    assert sprints.weekday_name("2026-06-12") == "friday"  # 2026-06-12 is a Friday


def test_autoreg_bands():
    assert sprints.autoreg(80)[0] == "green"
    assert sprints.autoreg(67)[0] == "green"        # boundary inclusive
    assert sprints.autoreg(50)[0] == "yellow"
    assert sprints.autoreg(34)[0] == "yellow"       # boundary inclusive
    assert sprints.autoreg(20)[0] == "red"
    assert sprints.autoreg(None)[0] == "unknown"


def test_autoreg_custom_thresholds():
    assert sprints.autoreg(60, green_min=55, yellow_min=30)[0] == "green"


def test_daily_override_supersedes_weekday_template():
    goals = {
        "weekly_plan": {"monday": {"sessions": ["gym hard"], "intensity": "hard", "hard": True}},
        "daily_overrides": {"2026-06-15": {"sessions": ["rest — travel"], "intensity": "easy", "recovery": True}},
    }
    n = sprints.normalize_goals(goals)
    # 2026-06-15 is a Monday; the override wins for that date only
    assert sprints.todays_plan(n, "monday", "2026-06-15")["sessions"] == ["rest — travel"]
    # a Monday WITHOUT an override falls back to the weekly template
    assert sprints.todays_plan(n, "monday", "2026-06-22")["sessions"] == ["gym hard"]
    # no today_iso → template (backward-compatible)
    assert sprints.todays_plan(n, "monday")["sessions"] == ["gym hard"]


def test_render_uses_daily_override():
    goals = {
        "weekly_plan": {"monday": {"sessions": ["gym hard"], "intensity": "hard", "hard": True}},
        "daily_overrides": {"2026-06-15": {"sessions": ["pool only"], "intensity": "easy", "recovery": True}},
    }
    out = sprints.render_training_section({"name": "S", "goals": goals}, "2026-06-15", 80)
    assert "pool only" in out and "gym hard" not in out
    assert "recovery day" in out  # override's recovery flag honored


def test_normalize_includes_daily_overrides_key():
    assert sprints.normalize_goals(None)["daily_overrides"] == {}
    assert sprints.normalize_goals(["fat loss"])["daily_overrides"] == {}


def test_parse_directives_from_rule():
    rules = ["Massage after hard days",
             "Autoregulate by WHOOP: Green=proceed; Yellow=downgrade hard->moderate; Red=pool+beach+massage only"]
    d = sprints.parse_autoreg_directives(rules)
    assert d["green"] == "proceed"
    assert d["yellow"] == "downgrade hard->moderate"
    assert d["red"] == "pool+beach+massage only"


def test_parse_directives_fallback_when_absent():
    assert sprints.parse_autoreg_directives(["just a normal rule"]) == sprints.DEFAULT_DIRECTIVES
    assert sprints.parse_autoreg_directives(None) == sprints.DEFAULT_DIRECTIVES


def test_autoreg_uses_supplied_directives():
    d = {"green": "send it", "yellow": "ease off", "red": "rest only"}
    assert sprints.autoreg(80, directives=d)[2] == "send it"
    assert sprints.autoreg(50, directives=d)[2] == "ease off"
    assert sprints.autoreg(10, directives=d)[2] == "rest only"


def test_render_directive_comes_from_rule():
    goals = dict(GOALS_V2)
    goals["rules"] = ["Autoregulate by WHOOP: Green=send it; Yellow=hold; Red=swim only"]
    out = sprints.render_training_section({"name": "S", "goals": goals}, "2026-06-15", 20)
    assert "swim only" in out and "pool + beach + massage only" not in out


def test_render_hard_day_with_recovery():
    sprint = {"name": "Phuket Sprint 2", "goals": GOALS_V2}
    out = sprints.render_training_section(sprint, "2026-06-15", 75)  # 2026-06-15 = Monday
    assert "🏋️ Training — Phuket Sprint 2" in out
    assert "Today (Monday): gym hard — hard intensity · HARD day" in out
    assert "🟢" in out and "proceed as planned" in out


def test_render_red_day_downgrade_directive():
    sprint = {"name": "S", "goals": GOALS_V2}
    out = sprints.render_training_section(sprint, "2026-06-15", 20)
    assert "🔴" in out and "pool + beach + massage only" in out


def test_render_adherence_ticks():
    sprint = {"name": "S", "goals": GOALS_V2}
    out = sprints.render_training_section(sprint, "2026-06-12", 70)
    assert "✅ gym" in out and "⬜ beach" in out and "✅ pool" in out


def test_render_rest_day_when_no_plan():
    sprint = {"name": "S", "goals": GOALS_V2}
    out = sprints.render_training_section(sprint, "2026-06-16", 70)  # Tuesday — not in plan
    assert "rest / unplanned" in out


def test_render_no_sprint_is_empty():
    assert sprints.render_training_section({}, "2026-06-12", 70) == ""
    assert sprints.render_training_section(None, "2026-06-12", 70) == ""


def test_render_legacy_array_no_weekly_plan():
    sprint = {"name": "Old", "goals": ["fat loss"]}
    out = sprints.render_training_section(sprint, "2026-06-12", 70)
    assert "rest / unplanned" in out  # no weekly_plan in legacy shape


def test_update_button():
    kb = sprints.update_button("2026-06-11")
    btn = kb["inline_keyboard"][0][0]
    assert btn["text"] == "📝 Update today"
    assert btn["callback_data"] == "menu:2026-06-11"


def test_adherence_keyboard_shape_and_callback_data():
    kb = sprints.adherence_keyboard("sprint-9", "2026-06-11", {"gym": True})
    rows = kb["inline_keyboard"]
    assert len(rows) == 2 and len(rows[0]) == 3 and len(rows[1]) == 2  # 3+2 layout
    flat = [b for row in rows for b in row]
    gym = next(b for b in flat if b["callback_data"].endswith(":gym"))
    assert gym["text"] == "✅ gym"  # already-done shows ✅
    assert gym["callback_data"] == "tick:sprint-9:2026-06-11:gym"
    pool = next(b for b in flat if b["callback_data"].endswith(":pool"))
    assert pool["text"] == "⬜ pool"
    # callback_data must stay within Telegram's 64-byte limit even with a real UUID
    import uuid
    long = sprints.adherence_keyboard(str(uuid.uuid4()), "2026-06-11", {})
    assert all(len(b["callback_data"].encode()) <= 64
               for row in long["inline_keyboard"] for b in row)


def test_mark_done_rejects_unknown_activity():
    import pytest
    with pytest.raises(ValueError):
        sprints.mark_done(MagicMock(), "sid", "2026-06-12", "yoga")


def test_mark_done_rest_via_dbrest():
    db = MagicMock()
    db._base = "https://x"  # DbRest marker
    db.select.return_value = [{"goals": GOALS_V2}]
    day = sprints.mark_done(db, "sprint-1", "2026-06-13", "gym")
    assert day["gym"] is True
    # wrote the whole goals object back, scoped by id
    args, kwargs = db.update.call_args
    assert args[0] == "sprints" and args[1] == {"id": "eq.sprint-1"}
    assert args[2]["goals"]["adherence_log"]["2026-06-13"]["gym"] is True


def test_mark_done_psycopg2_uses_jsonb_set():
    cur = MagicMock()
    cur.fetchone.return_value = ({"gym": True},)
    conn = MagicMock()
    conn.cursor.return_value = cur
    del conn._base  # force psycopg2 branch
    out = sprints.mark_done(conn, "sprint-1", "2026-06-13", "gym")
    assert out == {"gym": True}
    sql = cur.execute.call_args[0][0]
    assert "jsonb_set" in sql
    conn.commit.assert_called_once()
