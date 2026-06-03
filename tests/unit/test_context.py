"""Unit tests for lib/context.py — the per-person context-MD parser (v3-1).

Proves: section parsing, ';'-split multi-KV bullets, inline-comment stripping,
numeric/list/bool coercion, the minor-safety surface, and the headline blueprint
invariant — PC's calorie target is 2000 and Dea's is 2400 (read from the MD, not a
population default).
"""
from __future__ import annotations

import pytest

from lib.context import (
    parse_context_md,
    load_context,
    get_context,
    get_target,
    context_slug,
)

SAMPLE = """\
# Sample — HealthSpan Context
profile_id: 11111111-1111-1111-1111-111111111111   age: 13   sex: F   is_minor: true

## Who & why
A growing teen athlete.

## Targets / norms
- daily_calories: 2400        # growing — flag <2400 as LOW
- protein_g: 80 ; sleep_target_h: 9
- biomarker_priorities: hemoglobin, ferritin, vitamin_d
- training_focus: fun

## Coaching framing
- voice: supportive teen-athlete coach
- prescriptiveness: advisory

## Safety constraints
- NO adult supplement/hormone/fasting protocols.
- Growth & performance framing ONLY — never restriction language.
"""


class TestParser:
    def setup_method(self):
        self.ctx = parse_context_md(SAMPLE)

    def test_header_fields(self):
        assert self.ctx["name"] == "Sample"
        assert self.ctx["profile_id"] == "11111111-1111-1111-1111-111111111111"
        assert self.ctx["age"] == 13
        assert self.ctx["sex"] == "F"
        assert self.ctx["is_minor"] is True

    def test_numeric_target_coerced(self):
        assert self.ctx["targets"]["daily_calories"] == 2400
        assert isinstance(self.ctx["targets"]["daily_calories"], int)

    def test_semicolon_split_multi_kv(self):
        assert self.ctx["targets"]["protein_g"] == 80
        assert self.ctx["targets"]["sleep_target_h"] == 9

    def test_comma_list_value(self):
        assert self.ctx["targets"]["biomarker_priorities"] == [
            "hemoglobin", "ferritin", "vitamin_d"
        ]

    def test_inline_comment_stripped(self):
        # the '# growing...' comment must not leak into the value
        assert self.ctx["targets"]["daily_calories"] == 2400

    def test_coaching_and_safety(self):
        assert self.ctx["coaching"]["voice"] == "supportive teen-athlete coach"
        assert self.ctx["coaching"]["prescriptiveness"] == "advisory"
        assert len(self.ctx["safety"]) == 2
        assert any("never restriction" in s.lower() for s in self.ctx["safety"])

    def test_who_captured(self):
        assert "growing teen athlete" in self.ctx["who"].lower()

    def test_missing_target_is_none_not_default(self):
        # the blueprint rule: no entry → None (caller ASKS), never a substituted default
        assert get_target(self.ctx, "vo2max_target") is None


class TestRealContextFiles:
    """The shipped context files encode the headline blueprint invariant."""

    def test_pc_calorie_target_is_2000(self):
        ctx = load_context("pc")
        assert get_target(ctx, "daily_calories") == 2000
        assert ctx["is_minor"] is False

    def test_dea_calorie_target_is_2400_and_minor(self):
        ctx = load_context("dea")
        assert get_target(ctx, "daily_calories") == 2400
        assert ctx["is_minor"] is True
        # minor-safety must be present and forbid restriction framing
        assert ctx["safety"], "Dea's context must carry safety constraints"
        assert any("restriction" in s.lower() or "deficit" in s.lower()
                   for s in ctx["safety"])

    def test_dea_is_not_maintainer_pc_is(self):
        assert load_context("dea")["coaching"].get("is_maintainer") is False
        assert load_context("pc")["coaching"].get("is_maintainer") is True


class TestGetContext:
    def test_slug_from_display_name(self):
        assert context_slug({"display_name": "PC"}) == "pc"
        assert context_slug({"display_name": "Dea"}) == "dea"
        assert context_slug({"context_slug": "pc", "display_name": "X"}) == "pc"

    def test_get_context_matches_profile_id(self):
        cfg = {"display_name": "PC",
               "profile_id": "21f69003-46f8-4e1c-a928-b1f694ce4aff"}
        ctx = get_context(cfg)
        assert ctx["profile_id"] == cfg["profile_id"]

    def test_get_context_rejects_mismatched_profile_id(self):
        cfg = {"display_name": "PC", "profile_id": "deadbeef-0000-0000-0000-000000000000"}
        with pytest.raises(ValueError, match="wrong context file"):
            get_context(cfg)

    def test_missing_context_file_raises(self):
        with pytest.raises(FileNotFoundError, match="context MD not found"):
            load_context("nonexistent_person")
