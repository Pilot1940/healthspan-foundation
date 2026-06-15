"""Unit tests for lib/context.py — the per-person context-MD parser (v3-1).

Proves: section parsing, ';'-split multi-KV bullets, inline-comment stripping,
numeric/list/bool coercion, the minor-safety surface, and the headline blueprint
invariant — PC's calorie target is 2000 and Dea's is 2400 (read from the MD, not a
population default).
"""
from __future__ import annotations

import pytest

import lib.context as ctxmod
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

    def test_get_target_falls_through_to_coaching_and_safety(self):
        # coaching.voice / safety live outside ctx["targets"]; get_target must still
        # resolve them rather than wrongly returning None (which would make the skill ASK).
        ctx = {"targets": {"daily_calories": 2400},
               "coaching": {"voice": "warm"},
               "safety": ["no deficit talk"]}
        assert get_target(ctx, "daily_calories") == 2400
        assert get_target(ctx, "voice") == "warm"
        assert get_target(ctx, "safety") == ["no deficit talk"]
        assert get_target(ctx, "nonexistent") is None


class TestRealContextFiles:
    """The shipped context files encode the headline blueprint invariant."""

    def test_pc_calorie_target_from_context_not_a_default(self):
        # PC's daily_calories target is a DELIBERATE deficit (2100) for the Mera Peak fat-loss
        # plan — distinct from his maintenance/TDEE (2514, uVida) and from Dea's 2400. Read
        # from the context MD, proving per-person targets (not a population default).
        ctx = load_context("pc")
        assert get_target(ctx, "daily_calories") == 2100      # deficit target
        assert get_target(ctx, "maintenance_kcal") == 2514    # TDEE, reference only
        assert ctx["is_minor"] is False

    def test_dea_calorie_targets_and_safety_present(self):
        ctx = load_context("dea")
        # v2.2 (2026-06-15) restructured Dea's macros under ### sub-headings with a day-type
        # split: weekly-avg 2500, absolute floor 2400. The parser accrues ### sub-section
        # bullets into the "## Targets / norms" umbrella, so these are read (not silently lost).
        assert get_target(ctx, "daily_calories") == 2500     # weekly-avg target
        assert get_target(ctx, "calorie_floor") == 2400      # absolute floor — never below
        assert get_target(ctx, "protein_g") == 105
        # is_minor is false (PC-authorized adult framing, mig 072); protective safety
        # constraints are kept regardless of the minor flag.
        assert ctx["is_minor"] is False
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


# ── DB-first context (mig 073) ──────────────────────────────────────────────────

_PC_ID = "21f69003-46f8-4e1c-a928-b1f694ce4aff"
_DEA_ID = "3eed5503-a26f-4b88-bb76-075208fa5de3"

# A DB-sourced body that is DELIBERATELY different from SAMPLE (1234 kcal, not 2400) so a test
# can prove which tier served the context.
_DB_BODY = """\
# DBPerson — HealthSpan Context
profile_id: 21f69003-46f8-4e1c-a928-b1f694ce4aff   age: 45   sex: M   is_minor: false

## Targets / norms
- daily_calories: 1234
- protein_g: 200

## Coaching framing
- voice: db-sourced coach

## Safety constraints
- DB safety rule.
"""


class _FakeCursor:
    def __init__(self, body_by_id):
        self._body_by_id = body_by_id
        self._row = None
        self.last_params = None

    def execute(self, sql, params=None):
        self.last_params = params
        pid = params[0] if params else None
        self._row = (self._body_by_id[pid],) if pid in self._body_by_id else None

    def fetchone(self):
        return self._row


class _FakePsycopgConn:
    """Mimics the psycopg2 handle _read_context_from_db is given (it has .cursor)."""

    def __init__(self, body_by_id):
        self._body_by_id = body_by_id
        self.cursors = []

    def cursor(self):
        c = _FakeCursor(self._body_by_id)
        self.cursors.append(c)
        return c


class TestDbFirstContext:
    def _file(self, tmp_path, slug, body):
        (tmp_path / f"{slug}.context.md").write_text(body)
        return str(tmp_path)

    def test_db_first_wins_over_file(self, tmp_path):
        base = self._file(tmp_path, "pc", SAMPLE)              # file says 2400
        conn = _FakePsycopgConn({_PC_ID: _DB_BODY})           # DB says 1234
        ctx = load_context("pc", base_dir=base, conn=conn, profile_id=_PC_ID)
        assert ctx["_source"] == "db"
        assert ctx["targets"]["daily_calories"] == 1234       # DB wins, not the file's 2400

    def test_file_fallback_when_db_empty(self, tmp_path):
        base = self._file(tmp_path, "pc", SAMPLE)
        conn = _FakePsycopgConn({})                           # no row → None → fall to file
        ctx = load_context("pc", base_dir=base, conn=conn, profile_id=_PC_ID)
        assert ctx["_source"] == "file"
        assert ctx["targets"]["daily_calories"] == 2400

    def test_db_read_is_keyed_on_the_passed_profile_id(self, tmp_path):
        # Proves an RLS-scoped conn returns the caller's OWN row: the SELECT keys on the
        # profile_id we pass. The fake only knows Dea's id, so Dea reads, and a query for a
        # different id finds nothing (→ would fall back, here to a missing file → raises).
        conn = _FakePsycopgConn({_DEA_ID: _DB_BODY})
        ctx = load_context("dea", conn=conn, profile_id=_DEA_ID)
        assert ctx["_source"] == "db"
        assert conn.cursors[-1].last_params == (_DEA_ID,)
        with pytest.raises(FileNotFoundError):
            load_context("ghost", base_dir=str(tmp_path),
                         conn=_FakePsycopgConn({_DEA_ID: _DB_BODY}), profile_id=_PC_ID)

    def test_db_error_never_explodes_falls_to_file(self, tmp_path):
        base = self._file(tmp_path, "pc", SAMPLE)

        class _BoomConn:
            def cursor(self):
                raise RuntimeError("db down")

        ctx = load_context("pc", base_dir=base, conn=_BoomConn(), profile_id=_PC_ID)
        assert ctx["_source"] == "file"          # a DB hiccup must not block the file fallback

    def test_parse_output_shape_unchanged(self, tmp_path):
        base = self._file(tmp_path, "pc", SAMPLE)
        file_ctx = load_context("pc", base_dir=base)
        db_ctx = load_context("pc", base_dir=base,
                              conn=_FakePsycopgConn({_PC_ID: SAMPLE}), profile_id=_PC_ID)
        data_keys = lambda d: {k for k in d if not k.startswith("_")}
        assert data_keys(file_ctx) == data_keys(db_ctx)
        for k in ("name", "profile_id", "age", "sex", "is_minor", "who",
                  "targets", "coaching", "safety"):
            assert file_ctx[k] == db_ctx[k], f"{k} differs between file and DB source"


class TestGetContextFallbackChain:
    def test_db_first_via_get_context(self, tmp_path):
        cfg = {"display_name": "pc", "profile_id": _PC_ID}
        ctx = get_context(cfg, base_dir=str(tmp_path), conn=_FakePsycopgConn({_PC_ID: _DB_BODY}))
        assert ctx["_source"] == "db"
        assert ctx["targets"]["daily_calories"] == 1234

    def test_project_knowledge_fallback_when_no_db_no_file(self, tmp_path, monkeypatch):
        cfg = {"display_name": "ghost", "profile_id": _PC_ID}
        body = SAMPLE.replace("11111111-1111-1111-1111-111111111111", _PC_ID)
        monkeypatch.setattr(ctxmod, "_read_from_project_knowledge", lambda c: body)
        ctx = get_context(cfg, base_dir=str(tmp_path))          # empty dir, conn None
        assert ctx["_source"] == "project_knowledge"
        assert ctx["targets"]["daily_calories"] == 2400

    def test_project_knowledge_reads_inline_config_body(self, tmp_path):
        # the real (un-patched) helper picks up an inline config['context_md'] bootstrap
        body = SAMPLE.replace("11111111-1111-1111-1111-111111111111", _PC_ID)
        cfg = {"display_name": "ghost", "profile_id": _PC_ID, "context_md": body}
        ctx = get_context(cfg, base_dir=str(tmp_path))
        assert ctx["_source"] == "project_knowledge"

    def test_raises_when_no_source(self, tmp_path, monkeypatch):
        cfg = {"display_name": "ghost", "profile_id": _PC_ID}
        monkeypatch.setattr(ctxmod, "_read_from_project_knowledge", lambda c: None)
        with pytest.raises(RuntimeError, match="must not run on population defaults"):
            get_context(cfg, base_dir=str(tmp_path))
