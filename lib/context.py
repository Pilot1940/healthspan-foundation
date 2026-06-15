"""lib/context.py — the per-person CONTEXT layer (v3).

The engine is profile-agnostic. Everything personal — calorie/protein/sleep
targets, training focus, biomarker priorities, coaching voice, prescriptiveness,
and minor-safety constraints — is loaded read-only at session start. The skill takes
ALL targets/norms from here, NEVER a hardcoded or population default. No matching
entry → ask, don't assume.

Source of truth is **DB-first** (mig 073): ``profiles.context_md`` when an
authenticated session handle is available, falling back to the bundle-baked
``context/<slug>.context.md`` file, then to a project-knowledge inline bootstrap. A
maintainer editing the DB row therefore reaches every bundle on its next session
start with no rebundle. ``get_context`` records which tier served it in ``_source``
(``'db'`` | ``'file'`` | ``'project_knowledge'``).

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

    # Split into sections by ##/### headings. A ### sub-section's bullets ALSO accrue to its
    # parent ## "umbrella" — so targets authored under sub-headings (e.g. "### Energy & macros"
    # inside "## Targets / norms") are still found. Without this, a ### orphans those bullets
    # into their own section and _find() misses them = silent target loss. Shape is unchanged;
    # this only widens what bullets a given umbrella section sees.
    sections: dict[str, list[str]] = {}
    cur = None
    umbrella = None
    for ln in lines:
        m = _SECTION.match(ln)
        if m:
            title = m.group(1).strip().lower()
            stripped = ln.lstrip()
            level = len(stripped) - len(stripped.lstrip("#"))   # 2 for '##', 3 for '###'
            if title not in sections:
                sections[title] = []
            cur = title
            if level <= 2:
                umbrella = title
        elif cur is not None:
            sections[cur].append(ln)
            if umbrella is not None and umbrella != cur:
                sections[umbrella].append(ln)

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


def _read_context_from_db(conn, profile_id: str) -> str | None:
    """Read ``profiles.context_md`` for ``profile_id`` via whatever DB handle we're given
    (mig 073 — DB-backed context). Supports the three handles this codebase opens:

      * a **psycopg2 connection** (has ``.cursor``) — the direct_role / Cowork path,
      * a :class:`lib.db_rest.DbRest` (has ``.select``) — the supabase_client App/brief path,
      * a **supabase ``Client``** (has ``.table``) — the claude.ai App path.

    Returns the markdown body (str) or ``None`` (no row, NULL column, pre-073 DB, or any
    error). NEVER raises — a DB hiccup must fall through to the filesystem fallback, not
    explode the session bootstrap. RLS scopes the read to what the caller may see."""
    if conn is None or not profile_id:
        return None
    try:
        if hasattr(conn, "cursor"):                       # psycopg2 connection
            cur = conn.cursor()
            cur.execute("SELECT context_md FROM profiles WHERE id = %s", (profile_id,))
            row = cur.fetchone()
            return (row[0] or None) if row else None
        if hasattr(conn, "select"):                       # lib.db_rest.DbRest
            rows = conn.select(
                "profiles", select="context_md",
                filters={"id": f"eq.{profile_id}"}, limit=1,
            )
            return (rows[0].get("context_md") or None) if rows else None
        if hasattr(conn, "table"):                        # supabase Client
            res = conn.table("profiles").select("context_md").eq("id", profile_id).limit(1).execute()
            data = getattr(res, "data", None) or []
            return (data[0].get("context_md") or None) if data else None
    except Exception:
        return None
    return None


def _read_from_project_knowledge(config: dict) -> str | None:
    """Final fallback: a context body carried INLINE in the loaded config (the claude.ai
    project-knowledge / App bootstrap, where there is neither a DB handle nor a filesystem
    context file at session start). Returns the body or ``None``. Kept as a seam so the
    bootstrap source can grow without touching the fallback chain."""
    if not isinstance(config, dict):
        return None
    body = config.get("context_md")
    return body if (isinstance(body, str) and body.strip()) else None


def load_context(
    profile: str,
    base_dir: str | None = None,
    *,
    conn=None,
    profile_id: str | None = None,
) -> dict:
    """Load + parse a profile's context. ``profile`` is a slug (e.g. 'pc', 'dea').

    Source order (mig 073 — DB-first so a maintainer edit in Supabase propagates to every
    bundle on next session start):

      1. **DB** — ``profiles.context_md`` when a DB handle (``conn``) + ``profile_id`` are
         supplied. ``_source='db'``.
      2. **File** — ``context/<profile>.context.md`` (legacy bundles / dev / offline
         fallback). ``_source='file'``, ``_path`` set.

    Raises :class:`FileNotFoundError` if neither yields a body — a missing context is a hard
    stop (the skill must never run on population defaults). ``conn=None`` reproduces the
    historical file-only behaviour exactly (all existing callers stay correct)."""
    # 1. DB-first (best-effort — never lets a DB error block the file fallback)
    if conn is not None and profile_id:
        body = _read_context_from_db(conn, profile_id)
        if body:
            ctx = parse_context_md(body)
            ctx["_slug"] = profile
            ctx["_source"] = "db"
            return ctx

    # 2. Filesystem
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
    ctx["_source"] = "file"
    return ctx


def context_slug(config: dict) -> str:
    """Resolve the context slug from a loaded config: explicit ``context_slug`` wins,
    else the display_name lowercased ('PC' -> 'pc', 'Dea' -> 'dea')."""
    slug = config.get("context_slug")
    if slug:
        return str(slug).strip().lower()
    name = config.get("display_name") or ""
    return name.strip().lower()


def get_context(config: dict, base_dir: str | None = None, conn=None) -> dict:
    """Session-bootstrap entry point: load the context for the loaded config.

    Fallback chain (mig 073): **DB → filesystem → project-knowledge bootstrap**. Pass the
    already-authenticated session handle as ``conn`` (psycopg2 / DbRest / supabase Client) so
    the DB is the source of truth and a maintainer edit propagates without a rebundle; with
    ``conn=None`` it behaves exactly as before (file-first).

    Cross-checks the context's profile_id against the config's — a mismatch means the wrong
    context is wired and is a hard error (never coach one person with another's targets /
    safety rules)."""
    slug = context_slug(config)
    cfg_pid = config.get("profile_id")
    try:
        ctx = load_context(slug, base_dir=base_dir, conn=conn, profile_id=cfg_pid)
    except FileNotFoundError:
        # 3. project-knowledge bootstrap (no DB handle AND no filesystem context file)
        body = _read_from_project_knowledge(config)
        if not body:
            raise RuntimeError(
                f"No context found for profile {cfg_pid} (tried DB, filesystem, "
                f"project-knowledge) — the skill must not run on population defaults."
            )
        ctx = parse_context_md(body)
        ctx["_slug"] = slug
        ctx["_source"] = "project_knowledge"

    if ctx.get("profile_id") and cfg_pid and ctx["profile_id"] != cfg_pid:
        raise ValueError(
            f"context profile_id {ctx['profile_id']} != config profile_id {cfg_pid} "
            f"(source={ctx.get('_source')}, {ctx.get('_path')}) — wrong context file wired to this config."
        )
    return ctx


def get_target(ctx: dict, key: str):
    """Return a target/norm/coaching value or None. None means 'not specified' →
    the skill must ASK, never substitute a default.

    Searches `targets` first, then `coaching` (voice/tone/etc.), then the `safety`
    block — so e.g. get_target(ctx, "voice") and get_target(ctx, "safety") resolve
    from where they actually live in the context MD rather than always returning None."""
    targets = ctx.get("targets", {})
    if key in targets:
        return targets[key]
    coaching = ctx.get("coaching", {})
    if key in coaching:
        return coaching[key]
    if key == "safety":
        return ctx.get("safety") or None
    return None
