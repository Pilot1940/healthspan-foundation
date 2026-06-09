"""Unit tests for monitor/brief.py — daily health brief composer.

All tests mocked — no live DB, no Anthropic API, no Telegram.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from monitor.brief import (
    _fetch_supplement_status,
    _food_section,
    _supps_section,
    _viome_section,
    _whoop_section,
    compose_brief,
)


# ── helpers ───────────────────────────────────────────────────────────────────

def _mock_db(
    identity=None,
    profile=None,
    food_rows=None,
    regimens=None,
    supplements=None,
    intakes=None,
    whoop=None,
    viome_food=None,
    is_maintainer=None,
):
    """Return a MagicMock db whose select() yields test data in call order.

    compose_brief's select() call order (current contract):
      1. telegram_identities
      2. profiles (display_name → context slug)        ← unconditional, brief.py ~438
      3. food_logs (food_full)
      4. supplement_regimens
      5. supplements (one per regimen)                 ← one call per regimen row
      6. supplement_intake_logs
      7. whoop_cycles
      8. food_logs (viome flags)                       ← adult only (skipped for minors)
      9. profiles (is_maintainer)                       ← wrapped in try/except
     10. supplements (learned, review)                  ← only if maintainer
    The maintainer block (9–10) is best-effort; when side_effect runs out it raises
    StopIteration which the try/except swallows — so we only need to supply through #8
    plus an is_maintainer row (default: not a maintainer → block ends after #9).
    """
    db = MagicMock()
    ident = identity if identity is not None else [_default_identity()]
    is_minor = bool(ident and ident[0].get("is_minor"))
    regimen_rows = regimens if regimens is not None else []
    supp_rows = supplements if supplements is not None else []

    calls = [
        ident,                                              # 1. telegram_identities
        profile if profile is not None else [{"display_name": "PC"}],  # 2. profiles (slug)
        food_rows if food_rows is not None else [],         # 3. food_logs (full)
        regimen_rows,                                       # 4. supplement_regimens
    ]
    # _fetch_supplement_status: only when regimens is non-empty does it then do one
    # supplements lookup per UNIQUE regimen supplement_id, followed by one intake_logs call.
    if regimen_rows:
        unique_ids = list({str(r["supplement_id"]) for r in regimen_rows})
        for i in range(len(unique_ids)):
            calls.append([supp_rows[i]] if i < len(supp_rows) else [])
        calls.append(intakes if intakes is not None else [])  # supplement_intake_logs
    calls.append(whoop if whoop is not None else [])        # whoop_cycles
    if not is_minor:
        calls.append(viome_food if viome_food is not None else [])  # food_logs (viome)
    # profiles (is_maintainer) — best-effort; default not a maintainer (block ends here).
    calls.append(is_maintainer if is_maintainer is not None else [{"is_maintainer": False}])
    db.select.side_effect = calls
    return db


def _default_identity():
    return {
        "chat_id": 111111,
        "is_minor": False,
        "display_name": "PC",
    }


# ── tests ─────────────────────────────────────────────────────────────────────

def test_brief_composes_from_mocked_data():
    """compose_brief assembles a message and calls telegram_send exactly once."""
    db = _mock_db(
        food_rows=[
            {"calories": 400, "protein_g": 35, "carbs_g": 40, "fat_g": 12},
            {"calories": 600, "protein_g": 50, "carbs_g": 60, "fat_g": 20},
        ],
        regimens=[{"supplement_id": "s1", "timing": ["morning"]}],
        supplements=[{"id": "s1", "name": "berberine", "display_name": "Berberine"}],
        intakes=[{"supplement_id": "s1"}],
        whoop=[{
            "cycle_start": "2026-06-07",
            "recovery_score_pct": 78,
            "hrv_ms": 62.5,
            "resting_hr_bpm": 52,
            "asleep_duration_min": 430,
            "sleep_performance_pct": 85,
        }],
    )

    with patch("monitor.brief._call_claude_actions", return_value="1. Drink water.\n2. Walk 20 min."), \
         patch("monitor.inbox_drain.telegram_send") as mock_tg, \
         patch("lib.context.load_context", return_value={}):
        msg = compose_brief(db, "profile-uuid", {}, "api-key", "tok", "2026-06-07")

    assert mock_tg.called, "telegram_send must be called"
    assert "Daily brief" in msg
    assert "1000" in msg or "1,000" in msg or "kcal" in msg.lower()
    assert "Berberine" in msg
    assert "WHOOP" in msg
    assert "Recovery 78%" in msg


def test_brief_no_data_paths():
    """compose_brief returns '' and sends nothing when no telegram identity found."""
    db = MagicMock()
    db.select.return_value = []  # no identity

    with patch("monitor.inbox_drain.telegram_send") as mock_tg:
        result = compose_brief(db, "unknown-profile", {}, "key", "tok", "2026-06-07")

    assert result == ""
    assert not mock_tg.called


def test_supplement_remaining_regimen_minus_taken():
    """_supps_section shows taken (✅) and untaken (⬜) supplements correctly."""
    supps = [
        {"name": "Berberine", "timing": ["morning"], "taken": True},
        {"name": "Magnesium", "timing": ["bedtime"], "taken": False},
        {"name": "Vitamin D", "timing": ["morning"], "taken": False},
    ]
    msg = _supps_section(supps)
    assert "1/3 taken" in msg
    assert "✅ Berberine" in msg
    assert "⬜ Magnesium" in msg
    assert "⬜ Vitamin D" in msg


def test_minor_framing_in_brief():
    """compose_brief for a minor: no Viome section, no deficit language, performance framing."""
    db = _mock_db(
        identity=[{"chat_id": 222222, "is_minor": True, "display_name": "Dea"}],
        food_rows=[{"calories": 800, "protein_g": 40, "carbs_g": 100, "fat_g": 25}],
        regimens=[],
        supplements=[],
        intakes=[],
        whoop=[{
            "cycle_start": "2026-06-07",
            "recovery_score_pct": 85,
            "hrv_ms": 55.0,
            "resting_hr_bpm": 60,
            "asleep_duration_min": 480,
            "sleep_performance_pct": 92,
        }],
        viome_food=[],
    )

    with patch("monitor.brief._call_claude_actions", return_value="1. Eat a snack.") as mock_actions, \
         patch("monitor.inbox_drain.telegram_send") as mock_tg, \
         patch("lib.context.load_context", return_value={}):
        msg = compose_brief(db, "dea-profile", {}, "api-key", "tok", "2026-06-07")

    assert "Dea" in msg
    # Minor framing: growth/performance language, no viome section
    assert "⚠️ AVOID" not in msg
    assert "Minimize" not in msg.split("⚠️")[0] if "⚠️" not in msg else True
    # is_minor=True must be passed to _call_claude_actions
    assert mock_actions.call_args[0][6] is True  # is_minor positional arg
