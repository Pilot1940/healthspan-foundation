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
import os, re, sys, shutil, subprocess, zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NAME = "healthspan"


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


def main():
    version = sys.argv[1] if len(sys.argv) > 1 else "v3"
    _refresh_schema_map()          # ship a current schema map in every bundle
    stage = _stage(version)

    leaks = _leak_guard(stage)
    if leaks:
        shutil.rmtree(stage)
        print("LEAK GUARD FAILED — packaging aborted. Offending items:")
        for l in leaks:
            print("  -", l)
        sys.exit(1)

    out = os.path.join(ROOT, "dist", f"{NAME}-{version}.skill")
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
    print(f"OK leak guard: no secrets or person-specific context files staged")
    print(f"packaged {n} files → {out}  ({size:,} bytes)")
    print("manifest (top level):")
    for entry in sorted(os.listdir(stage)):
        print("  ", entry)
    shutil.rmtree(stage)


if __name__ == "__main__":
    main()
