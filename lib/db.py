"""lib/db.py — HealthSpan DB connection + profile resolution.

Reads the ``healthspan`` entry from ``.db-config.json`` (the BaySysAI shared
db-config, key path ``databases.healthspan.DATABASE_URL``).

SECURITY: the connection string is a secret. It is read straight from the
config file and handed to libpq via psycopg2.connect. It is never logged,
printed, returned to callers, or embedded in exception messages.

``psycopg2`` is imported LAZILY (only in the direct_role / local Postgres path) so the
pure ``supabase_client`` (App / claude.ai) path imports this module — and signs in —
without psycopg2 installed.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

# ``~/Dropbox`` is a convenience symlink that is absent on some machines; the
# real Dropbox mount is under ``~/Library/CloudStorage/Dropbox``. Try both, and
# allow an explicit override for tests / non-standard installs.
_CONFIG_ENV = "HEALTHSPAN_DB_CONFIG"
_CONFIG_CANDIDATES = (
    "~/Dropbox/Development/BaySysAI/.db-config.json",
    "~/Library/CloudStorage/Dropbox/Development/BaySysAI/.db-config.json",
)
_DB_KEY = "healthspan"

_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def _config_path() -> Path:
    override = os.environ.get(_CONFIG_ENV)
    candidates = [override] if override else list(_CONFIG_CANDIDATES)
    for cand in candidates:
        if not cand:
            continue
        p = Path(cand).expanduser()
        if p.is_file():
            return p
    raise FileNotFoundError(
        f"db-config.json not found (looked in: {', '.join(c for c in candidates if c)}). "
        f"Set ${_CONFIG_ENV} to point at it."
    )


def _healthspan_conf() -> dict:
    with _config_path().open() as f:
        cfg = json.load(f)
    try:
        return cfg["databases"][_DB_KEY]
    except (KeyError, TypeError) as e:
        raise KeyError(f"no 'databases.{_DB_KEY}' entry in db-config.json") from e


def _dsn() -> str:
    """Return the connection string. SECRET — internal use only."""
    dsn = _healthspan_conf().get("DATABASE_URL")
    if not dsn:
        raise KeyError(f"'databases.{_DB_KEY}' has no DATABASE_URL")
    return dsn


def _psycopg2():
    """Import psycopg2 on demand. Only the direct_role / local Postgres path needs it;
    the supabase_client (App) path never calls this. Raises an actionable error if the
    direct_role path is used without psycopg2 installed."""
    try:
        import psycopg2
        return psycopg2
    except ImportError as e:
        raise RuntimeError(
            "psycopg2 is required for direct_role (local Postgres) mode but is not "
            "installed — run: pip install psycopg2-binary"
        ) from e


def get_conn(*, readonly: bool = False):
    """Open a new psycopg2 connection to the HealthSpan DB.

    The DSN is read from ``.db-config.json`` and passed straight to libpq; it is
    never exposed. The caller owns the connection lifecycle (commit/rollback/close).
    """
    conn = _psycopg2().connect(_dsn())
    if readonly:
        conn.set_session(readonly=True)
    return conn


def resolve_profile(conn, who: str | None = None) -> str:
    """Resolve a profile to its UUID, by id or display_name. Defaults to 'PC'.

    * ``who`` looking like a UUID → validated to exist, returned as-is.
    * otherwise treated as a ``display_name`` (case-insensitive).

    Raises :class:`LookupError` if the profile is missing or the name is
    ambiguous. Never guesses.
    """
    who = "PC" if who is None else who
    if isinstance(who, str):
        who = who.strip()

    cur = conn.cursor()
    if isinstance(who, str) and _UUID_RE.match(who):
        cur.execute("SELECT id FROM profiles WHERE id = %s", (who,))
        row = cur.fetchone()
        if not row:
            raise LookupError(f"no profile with id={who}")
        return str(row[0])

    cur.execute(
        "SELECT id FROM profiles WHERE lower(display_name) = lower(%s)", (who,)
    )
    rows = cur.fetchall()
    if not rows:
        raise LookupError(f"no profile with display_name={who!r}")
    if len(rows) > 1:
        raise LookupError(
            f"display_name={who!r} is ambiguous ({len(rows)} matches) — pass the UUID"
        )
    return str(rows[0][0])


# ============================================================================
# Access Model A — per-instance skill connection (PROMPT 14: support BOTH paths)
# A2 (direct_role): psycopg2 as healthspan_app → SET ROLE authenticated + set the
#                   JWT claim so RLS/has_profile_access scopes to this profile.
# A1 (supabase_client): supabase-py with the person's auth session (JWT carries
#                   auth.uid() → RLS automatic). Best for Dea/mobile/Telegram.
# Driven by healthspan.config.json -> connection.mode.
# ============================================================================
import json as _json


def load_config(path: str) -> dict:
    """Load a per-instance healthspan.config.json."""
    with open(path) as f:
        return _json.load(f)


def _set_profile_claim(conn, auth_user_id: str) -> None:
    """A2: assume authenticated + set the JWT claim so has_profile_access() resolves.
    Must run on every fresh connection/transaction before any data query."""
    cur = conn.cursor()
    cur.execute("SET ROLE authenticated")
    cur.execute(
        "SELECT set_config('request.jwt.claims', %s, false)",
        (_json.dumps({"sub": auth_user_id, "role": "authenticated"}),),
    )


def get_app_connection(config: dict):
    """Return a profile-scoped, ALREADY-AUTHENTICATED handle per ``connection.mode``.

    A2 -> (psycopg2 connection, "psycopg2"); already SET ROLE authenticated with the
          profile's JWT claim applied — RLS active, DELETE/DDL denied at the grant level.
    A1 -> (supabase Client, "supabase"); **signed in here** with the person's email +
          password from the config, so the client carries a real JWT (auth.uid()) and RLS
          scopes automatically. The caller does NOTHING extra — no second sign-in step.

    The caller owns lifecycle. For A2, re-call _set_profile_claim() after any ROLE reset.
    """
    conn_cfg = config.get("connection", {})
    mode = conn_cfg.get("mode")

    if mode == "direct_role":
        dr = conn_cfg["direct_role"]
        conn = _psycopg2().connect(dr["db_url"])        # healthspan_app credential
        _set_profile_claim(conn, dr["auth_user_id"])    # scope to this profile
        return conn, "psycopg2"

    if mode == "supabase_client":
        from supabase import create_client
        sc = conn_cfg["supabase_client"]
        email = sc.get("auth_email")
        password = sc.get("auth_password")
        # Actionable config errors — NEVER fall through to an unauthenticated (anon) client,
        # which RLS denies (42501 permission denied) on every read.
        if not password:
            raise ValueError(
                "config.connection.supabase_client.auth_password required for App mode"
            )
        if not email:
            raise ValueError(
                "config.connection.supabase_client.auth_email required for App mode"
            )
        client = create_client(sc["supabase_url"], sc["supabase_anon_key"])
        # Sign in HERE so the client holds the person's JWT; RLS then scopes every query.
        try:
            res = client.auth.sign_in_with_password({"email": email, "password": password})
        except Exception as e:
            # surface the auth failure — do not return an anon client
            raise RuntimeError(f"supabase_client sign-in failed for {email!r}: {e}") from e
        session = getattr(res, "session", None)
        if session is None or not getattr(session, "access_token", None):
            raise RuntimeError(
                f"supabase_client sign-in returned no session/JWT for {email!r} — "
                "check auth_email / auth_password"
            )
        return client, "supabase"

    raise ValueError(f"connection.mode must be 'direct_role' or 'supabase_client', got {mode!r}")
