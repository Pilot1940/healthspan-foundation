"""package_skill.py — assemble a transportable `<name>.skill` zip (V3-7 packaging).

A .skill is a zip with a single top-level dir: SKILL.md + CHANGELOG.md + the runtime code
the skill imports + docs + the EXAMPLE config. It deliberately EXCLUDES every secret-bearing
or person-specific artefact — .env, filled per-instance configs, role secrets, DB backups,
AND the per-person context/ files (the bundle is person-AGNOSTIC; config + context are
delivered per-person, never baked in). A LEAK GUARD aborts if any credential value OR any
person-specific context file slips into the staging tree.

Usage:  python scripts/package_skill.py [version]   # default version 'v3'
Output: dist/healthspan-<version>.skill  (+ a printed manifest)
"""
import os, re, sys, json, shutil, subprocess, zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NAME = "healthspan"

# Prepended to SKILL.md ONLY in an unrestricted (privileged) bundle — never committed to
# the shared SKILL.md (it would be false in a restricted bundle). Injected post-guard.
_UNRESTRICTED_HEADER = (
    "> # ⚠️ UNRESTRICTED MAINTAINER BUNDLE\n"
    "> This bundle's config carries a **privileged `postgres` credential**. The connection\n"
    "> **BYPASSES RLS** and can **DELETE / TRUNCATE / DDL on EVERY profile** — not just the\n"
    "> owner's. The §7 \"role cannot DELETE/DDL\" / \"cannot cross-profile\" guarantees DO NOT\n"
    "> apply here. **Never share, sync, copy, commit, or paste this bundle or its config.**\n"
    "> It is for the maintainer's own machine only.\n\n"
)


def _resolve_secrets(obj):
    """Recursively resolve `@secret:PATH` / `@secret:PATH#KEY` string sentinels by reading
    the named secret file AT BUILD TIME. `#KEY` reads the value of a `KEY=…` or `KEY: …`
    line; no `#` reads the whole file (stripped). The packager — not a human — touches the
    credential, and only when assembling a person bundle into the gitignored dist/ zip."""
    if isinstance(obj, dict):
        return {k: _resolve_secrets(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_resolve_secrets(v) for v in obj]
    if isinstance(obj, str) and obj.startswith("@secret:"):
        spec = obj[len("@secret:"):]
        path, _, key = spec.partition("#")
        full = path if os.path.isabs(path) else os.path.join(ROOT, path)
        if not os.path.isfile(full):
            raise FileNotFoundError(
                f"secret file not found for sentinel {obj!r}: {full} — "
                "create it (gitignored) with the credential before building this bundle"
            )
        text = open(full, encoding="utf-8").read()
        if not key:
            return text.strip()
        for line in text.splitlines():
            line = line.strip()
            for sep in ("=", ":"):
                if line.startswith(key + sep):
                    return line[len(key) + 1:].strip()
        raise KeyError(f"key {key!r} not found in secret file {path}")
    return obj


def _inject_person(stage, person_cfg_path, unrestricted):
    """POST-GUARD: assemble the person's config (resolving @secret sentinels) and write it +
    their context into the staged tree, which is then zipped into dist/ (gitignored). The
    leak guard has ALREADY run on the base bundle; the credential never reaches the guard
    scan and never reaches git. This is the owner's personal install artifact."""
    with open(person_cfg_path, encoding="utf-8") as f:
        cfg = json.load(f)
    cfg = _resolve_secrets(cfg)
    slug = str(cfg.get("display_name", "user")).strip().lower().split()[0]
    os.makedirs(os.path.join(stage, "config"), exist_ok=True)
    cfg_rel = os.path.join("config", f"{slug}.config.json")
    with open(os.path.join(stage, cfg_rel), "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    injected = [cfg_rel]
    ctx_src = os.path.join(ROOT, "context", f"{slug}.context.md")
    if os.path.isfile(ctx_src):
        os.makedirs(os.path.join(stage, "context"), exist_ok=True)
        ctx_rel = os.path.join("context", f"{slug}.context.md")
        shutil.copy2(ctx_src, os.path.join(stage, ctx_rel))
        injected.append(ctx_rel)
    if unrestricted:
        skill_md = os.path.join(stage, "SKILL.md")
        body = open(skill_md, encoding="utf-8").read()
        with open(skill_md, "w", encoding="utf-8") as f:
            f.write(_UNRESTRICTED_HEADER + body)
    return injected


def _refresh_schema_map():
    """Regenerate docs/SCHEMA-MAP.md from live column COMMENTs so every bundle ships a
    CURRENT map (the schema-map staleness is what makes a tester guess wrong column names).
    Best-effort: if the DB/.env is unavailable (offline packaging), warn and ship the
    committed map rather than hard-failing."""
    gen = os.path.join(ROOT, "scripts", "gen_schema_map.py")
    try:
        r = subprocess.run([sys.executable, gen], cwd=ROOT, capture_output=True, text=True, timeout=60)
        if r.returncode == 0:
            print("OK schema-map: regenerated from live COMMENTs")
        else:
            print("WARN schema-map: regen failed — shipping the committed docs/SCHEMA-MAP.md")
            print("     " + (r.stderr.strip().splitlines() or ["(no stderr)"])[-1])
    except Exception as e:
        print(f"WARN schema-map: regen skipped ({type(e).__name__}) — shipping committed map")

# Runtime + docs the skill needs — copied wholesale (minus the prune list below).
# NB: context/ is intentionally EXCLUDED (same as config/) — the bundle is person-agnostic;
# per-person context.md is delivered separately (repo folder on Cowork/Code, or Project
# knowledge on claude.ai). Only the EXAMPLE config ships, as a format reference.
INCLUDE_DIRS = ["lib", "plan", "monitor", "analysis", "ingest", "export",
                "migrations", "docs", "scripts"]
INCLUDE_FILES = ["SKILL.md", "CHANGELOG.md", "requirements.txt",
                 "config/healthspan.config.example.json"]

# Never package these (secrets / personal data / noise).
PRUNE_NAMES = {"__pycache__", ".pytest_cache", ".venv", ".git", ".DS_Store",
               "backups", "dist", "node_modules", ".temp"}
PRUNE_SUFFIX = (".pyc", ".pyo")

# A staged file is a SECRET LEAK if it matches a real config/secret path, or its CONTENT
# carries a credential value (not an <…> placeholder).
SECRET_PATH = re.compile(r"(^|/)(\.env$|config/.*\.config\.json$|.*\.secret.*)")
SECRET_PATH_ALLOW = re.compile(r"config/.*\.example\.json$")
# Per-person context files must NEVER ship — the bundle is person-agnostic. Any staged
# context/<who>.context.md is a leak (a `.example.md` template would be allowed).
CONTEXT_PATH = re.compile(r"(^|/)context/.*\.context\.md$")
CONTEXT_PATH_ALLOW = re.compile(r"\.example\.md$")
# Match only REAL credential shapes — long tokens / actual JWTs — never placeholders
# like 'user:pass@', '[db-password]', or '<password>' (those are doc examples), and not
# this guard's own short regex literals.
SECRET_CONTENT = re.compile(
    r"hsapp_[A-Za-z0-9._-]{12,}"                       # the healthspan_app DB password
    r"|sbp_[A-Za-z0-9]{12,}"                            # supabase personal access token
    r"|eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}"      # a real JWT (anon / service_role key)
)


def _prune(path):
    base = os.path.basename(path)
    return base in PRUNE_NAMES or base.endswith(PRUNE_SUFFIX)


def _stage(version):
    stage = os.path.join(ROOT, "dist", f"{NAME}")
    if os.path.exists(stage):
        shutil.rmtree(stage)
    os.makedirs(stage)

    def copy_into(rel):
        src = os.path.join(ROOT, rel)
        if not os.path.exists(src):
            return
        dst = os.path.join(stage, rel)
        if os.path.isdir(src):
            for dirpath, dirnames, filenames in os.walk(src):
                dirnames[:] = [d for d in dirnames if not _prune(os.path.join(dirpath, d))]
                for fn in filenames:
                    if _prune(fn):
                        continue
                    s = os.path.join(dirpath, fn)
                    d = os.path.join(stage, os.path.relpath(s, ROOT))
                    os.makedirs(os.path.dirname(d), exist_ok=True)
                    shutil.copy2(s, d)
        else:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)

    for d in INCLUDE_DIRS:
        copy_into(d)
    for f in INCLUDE_FILES:
        copy_into(f)
    return stage


def _leak_guard(stage):
    """Abort if any staged file is a secret path or contains a credential value."""
    leaks = []
    for dirpath, _, filenames in os.walk(stage):
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, stage)
            if SECRET_PATH.search(rel) and not SECRET_PATH_ALLOW.search(rel):
                leaks.append(f"secret path staged: {rel}")
                continue
            if CONTEXT_PATH.search(rel) and not CONTEXT_PATH_ALLOW.search(rel):
                leaks.append(f"person-specific context file staged: {rel}")
                continue
            try:
                with open(full, encoding="utf-8", errors="ignore") as fh:
                    txt = fh.read()
            except Exception:
                continue
            for m in SECRET_CONTENT.finditer(txt):
                leaks.append(f"credential value in {rel}: …{m.group(0)[:24]}…")
    return leaks


def _parse_args(argv):
    version, person, out, unrestricted = "v3", None, None, False
    rest, i = [], 0
    while i < len(argv):
        a = argv[i]
        if a == "--person":
            person = argv[i + 1]; i += 2
        elif a == "--out":
            out = argv[i + 1]; i += 2
        elif a == "--unrestricted":
            unrestricted = True; i += 1
        else:
            rest.append(a); i += 1
    if rest:
        version = rest[0]
    return version, person, out, unrestricted


def main():
    version, person_cfg, out_name, unrestricted = _parse_args(sys.argv[1:])
    if unrestricted and not person_cfg:
        sys.exit("--unrestricted requires --person CONFIG (the privileged config to inject)")
    _refresh_schema_map()          # ship a current schema map in every bundle
    stage = _stage(version)

    # Leak guard runs on the BASE bundle (no person config/context staged yet). This is the
    # repo-hygiene gate — it must always pass and is never weakened for a person bundle.
    leaks = _leak_guard(stage)
    if leaks:
        shutil.rmtree(stage)
        print("LEAK GUARD FAILED — packaging aborted. Offending items:")
        for l in leaks:
            print("  -", l)
        sys.exit(1)
    print("OK leak guard: no secrets or person-specific context files staged")

    # POST-GUARD person injection (optional): the assembled config (with resolved secrets)
    # + context land ONLY in the staged tree we are about to zip into dist/ (gitignored).
    # They are never re-scanned and never committed. This is the owner's install artifact.
    injected = []
    if person_cfg:
        injected = _inject_person(stage, person_cfg, unrestricted)
        print(f"OK injected person bundle ({'UNRESTRICTED · ' if unrestricted else ''}"
              f"post-guard, dist-only): " + ", ".join(injected))

    out = os.path.join(ROOT, "dist", out_name or f"{NAME}-{version}.skill")
    if os.path.exists(out):
        os.remove(out)
    n = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for dirpath, _, filenames in os.walk(stage):
            for fn in sorted(filenames):
                full = os.path.join(dirpath, fn)
                arc = os.path.join(NAME, os.path.relpath(full, stage))
                z.write(full, arc)
                n += 1
    size = os.path.getsize(out)
    print(f"packaged {n} files → {out}  ({size:,} bytes)")
    if injected:
        print("  ⚠️ this bundle carries a per-person credential — do not share or commit it")
    print("manifest (top level):")
    for entry in sorted(os.listdir(stage)):
        print("  ", entry)
    shutil.rmtree(stage)


if __name__ == "__main__":
    main()
