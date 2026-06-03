"""lib/context.py — the per-person CONTEXT-MD layer (v3).

The engine is profile-agnostic. Everything personal — calorie/protein/sleep
targets, training focus, biomarker priorities, coaching voice, prescriptiveness,
and minor-safety constraints — lives in a human-authored ``context/<slug>.context.md``
and is loaded read-only at session start. The skill takes ALL targets/norms from
here, NEVER a hardcoded or population default. No matching entry → ask, don't assume.

Parsed structure (see HealthSpan-Skill-v3-Build-Blueprint.md §2):

    # <Name> — HealthSpan Context
    profile_id: <uuid>   age: <n>   sex: <M/F>   is_minor: <bool>

    ## Who & why
    <free text>

    ## Targets / norms
    - daily_calories: 2000        # inline comments are stripped
    - protein_g: 160 ; sleep_target_h: 8        # multiple k:v per bullet, ';'-split
    - biomarker_priorities: HbA1c, fasting_insulin, ApoB, hsCRP   # comma list → list

    ## Coaching framing
    - voice: evidence-based longevity coach
    - prescriptiveness: high

    ## Safety constraints
    - NO adult supplement/hormone/fasting protocols.

Returns a dict: {name, profile_id, age, sex, is_minor, who, targets{}, coaching{}, safety[]}.
"""
from __future__ import annotations

import os
import re

_HEADER_KV = re.compile(r"(\w+)\s*:\s*([^\s].*?)(?=\s+\w+\s*:|$)")
_SECTION = re.compile(r"^\s*#{2,3}\s+(.*?)\s*$")
_H1 = re.compile(r"^\s*#\s+(.*?)\s*$")


def _strip_comment(s: str) -> str:
    """Drop an inline ``# comment`` (but keep a leading '#', which is a heading)."""
    # only strip ' #...' that follows content, not a line that *starts* with #
    i = s.find("#")
    return (s[:i] if i > 0 else s).strip()


def _coerce(v: str):
    """'2000' -> 2000, '7.5' -> 7.5, 'a, b, c' -> ['a','b','c'], else trimmed str."""
    v = v.strip()
    if not v:
        return None
    try:
        f = float(v)
        return int(f) if f.is_integer() else f
    except ValueError:
        pass
    if "," in v:
        return [p.strip() for p in v.split(",") if p.strip()]
    if v.lower() in ("true", "yes"):
        return True
    if v.lower() in ("false", "no"):
        return False
    return v


def _section_bullets(lines: list[str]) -> list[str]:
    """Return the bullet bodies ('- foo' -> 'foo') within a block of lines."""
    out = []
    for ln in lines:
        s = ln.strip()
        if s.startswith(("- ", "* ")):
            out.append(s[2:].strip())
    return out


def parse_context_md(text: str) -> dict:
    """Parse a context MD string into the structured context dict. Pure, no IO."""
    lines = text.splitlines()

    ctx: dict = {
        "name": None, "profile_id": None, "age": None, "sex": None,
        "is_minor": None, "who": "", "targets": {}, "coaching": {}, "safety": [],
    }

    # H1 title → name ("# Dea — HealthSpan Context" → "Dea")
    for ln in lines:
        m = _H1.match(ln)
        if m:
            ctx["name"] = m.group(1).split("—")[0].split("-")[0].strip() or None
            break

    # Header KV line (profile_id / age / sex / is_minor), wherever it sits pre-section
    for ln in lines:
        if _SECTION.match(ln):
            break
        if "profile_id:" in ln or "age:" in ln or "is_minor:" in ln:
            for k, v in _HEADER_KV.findall(ln):
                kl = k.lower()
                if kl in ("profile_id", "sex"):
                    ctx[kl] = v.strip()
                elif kl == "age":
                    ctx["age"] = _coerce(v)
                elif kl == "is_minor":
                    ctx["is_minor"] = _coerce(v)

    # Split into sections by ##/### headings
    sections: dict[str, list[str]] = {}
    cur = None
    for ln in lines:
        m = _SECTION.match(ln)
        if m:
            cur = m.group(1).strip().lower()
            sections[cur] = []
        elif cur is not None:
            sections[cur].append(ln)

    def _find(*names):
        for key in sections:
            for n in names:
                if n in key:
                    return sections[key]
        return []

    # Who & why
    who = _find("who")
    ctx["who"] = " ".join(l.strip() for l in who if l.strip()).strip()

    # Targets / norms — each bullet may carry several ';'-separated key:value pairs
    for bullet in _section_bullets(_find("target", "norm")):
        body = _strip_comment(bullet)
        for seg in body.split(";"):
            seg = seg.strip()
            if not seg or ":" not in seg:
                continue
            k, _, v = seg.partition(":")
            key = k.strip().lower().replace(" ", "_")
            ctx["targets"][key] = _coerce(v)

    # Coaching framing
    for bullet in _section_bullets(_find("coaching", "framing")):
        body = _strip_comment(bullet)
        if ":" in body:
            k, _, v = body.partition(":")
            ctx["coaching"][k.strip().lower().replace(" ", "_")] = _coerce(v)

    # Safety constraints — free-text bullets, kept verbatim (order preserved)
    ctx["safety"] = [_strip_comment(b) for b in _section_bullets(_find("safety"))]

    return ctx


_DEF_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "context")


def load_context(profile: str, base_dir: str | None = None) -> dict:
    """Read ``context/<profile>.context.md`` and parse it. ``profile`` is a slug
    (e.g. 'pc', 'dea'). Raises FileNotFoundError if the file is absent — a missing
    context file is a hard stop (the skill must not run on population defaults)."""
    base = base_dir or _DEF_DIR
    path = os.path.join(base, f"{profile}.context.md")
    if not os.path.isfile(path):
        raise FileNotFoundError(
            f"context MD not found: {path}. Every profile needs a context file — "
            f"the skill reads all targets from it, never a population default."
        )
    with open(path) as f:
        ctx = parse_context_md(f.read())
    ctx["_path"] = path
    ctx["_slug"] = profile
    return ctx


def context_slug(config: dict) -> str:
    """Resolve the context slug from a loaded config: explicit ``context_slug`` wins,
    else the display_name lowercased ('PC' -> 'pc', 'Dea' -> 'dea')."""
    slug = config.get("context_slug")
    if slug:
        return str(slug).strip().lower()
    name = config.get("display_name") or ""
    return name.strip().lower()


def get_context(config: dict, base_dir: str | None = None) -> dict:
    """Session-bootstrap entry point: load the context MD for the loaded config.

    Cross-checks the context file's profile_id against the config's — a mismatch
    means the wrong context file is wired and is a hard error (never coach one
    person with another's targets / safety rules)."""
    ctx = load_context(context_slug(config), base_dir=base_dir)
    cfg_pid = config.get("profile_id")
    if ctx.get("profile_id") and cfg_pid and ctx["profile_id"] != cfg_pid:
        raise ValueError(
            f"context profile_id {ctx['profile_id']} != config profile_id {cfg_pid} "
            f"({ctx.get('_path')}) — wrong context file wired to this config."
        )
    return ctx


def get_target(ctx: dict, key: str):
    """Return a target/norm value or None. None means 'not specified' → the skill
    must ASK, never substitute a default."""
    return ctx.get("targets", {}).get(key)
