"""ingest/whoop_oauth.py — one-time local WHOOP OAuth2 consent flow.

Auth is authorization_code ONLY — client_credentials is rejected by WHOOP.
Credentials (WHOOP_CLIENT_ID, WHOOP_CLIENT_SECRET) live in the gitignored .env.
The refresh token is written back to .env (moves to Supabase Vault in a later step).

Idempotent: if WHOOP_REFRESH_TOKEN is already in .env and still valid (tested by
attempting a token refresh), the consent flow is skipped entirely.

Usage:
    python -m ingest.whoop_oauth          # from repo root
    python ingest/whoop_oauth.py
"""
from __future__ import annotations

import base64
import os
import secrets
import sys
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

_AUTH_URL = "https://api.prod.whoop.com/oauth/oauth2/auth"
_TOKEN_URL = "https://api.prod.whoop.com/oauth/oauth2/token"
_REDIRECT_URI = "http://localhost:8080/callback"
_SCOPES = (
    "read:recovery read:cycles read:workout "
    "read:sleep read:profile read:body_measurement"
)
_CALLBACK_PORT = 8080
_CONSENT_TIMEOUT_SEC = 120


# ---------------------------------------------------------------------------
# .env helpers (no external deps — raw file editing)
# ---------------------------------------------------------------------------

def _load_env() -> dict:
    envp = ROOT / ".env"
    env: dict[str, str] = {}
    if not envp.is_file():
        return env
    for line in envp.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip()
    return env


def _write_env_key(key: str, value: str) -> None:
    """Update or append a key in .env without touching other lines."""
    envp = ROOT / ".env"
    if not envp.is_file():
        envp.write_text(f"{key}={value}\n")
        return
    lines = envp.read_text().splitlines(keepends=True)
    found = False
    with envp.open("w") as f:
        for line in lines:
            if line.startswith(f"{key}="):
                f.write(f"{key}={value}\n")
                found = True
            else:
                f.write(line)
        if not found:
            if lines and not lines[-1].endswith("\n"):
                f.write("\n")
            f.write(f"{key}={value}\n")


# ---------------------------------------------------------------------------
# Token exchange
# ---------------------------------------------------------------------------

def _basic_auth_header(client_id: str, client_secret: str) -> str:
    """WHOOP token endpoint requires credentials as HTTP Basic auth, not in the body."""
    encoded = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    return f"Basic {encoded}"


def _exchange_code(code: str, client_id: str, client_secret: str) -> dict:
    import urllib.request
    import json
    # Send credentials both in Basic auth header AND in body — WHOOP accepts either
    body = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": _REDIRECT_URI,
        "client_id": client_id,
        "client_secret": client_secret,
    }).encode()
    req = urllib.request.Request(_TOKEN_URL, data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    req.add_header("Authorization", _basic_auth_header(client_id, client_secret))
    req.add_header("User-Agent", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        err_body = e.read().decode(errors="replace")
        raise RuntimeError(f"WHOOP token exchange HTTP {e.code}: {err_body}") from e


def try_refresh(client_id: str, client_secret: str, refresh_token: str) -> tuple[str, str] | None:
    """Try to exchange a refresh token for new tokens. Returns (access, refresh) or None."""
    import urllib.request
    import json
    body = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }).encode()
    req = urllib.request.Request(_TOKEN_URL, data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    req.add_header("Authorization", _basic_auth_header(client_id, client_secret))
    req.add_header("User-Agent", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
            return data["access_token"], data["refresh_token"]
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Local callback server
# ---------------------------------------------------------------------------

class _CallbackHandler(BaseHTTPRequestHandler):
    """Minimal HTTP handler — captures the ?code= from the WHOOP redirect."""

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if not parsed.path.startswith("/callback"):
            self.send_response(404); self.end_headers()
            return
        params = urllib.parse.parse_qs(parsed.query)
        if "error" in params:
            self.server.auth_code = None  # type: ignore[attr-defined]
            err = params.get("error", ["unknown"])[0]
            self.send_response(400); self.end_headers()
            self.wfile.write(f"Error from WHOOP: {err}".encode())
        elif "code" in params:
            # Verify state to prevent CSRF
            returned_state = (params.get("state") or [""])[0]
            if returned_state != getattr(self.server, "oauth_state", ""):
                self.server.auth_code = None  # type: ignore[attr-defined]
                self.send_response(400); self.end_headers()
                self.wfile.write(b"State mismatch - possible CSRF. Try again.")
                return
            self.server.auth_code = params["code"][0]  # type: ignore[attr-defined]
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"WHOOP authorisation successful. You can close this tab.")
        else:
            self.send_response(400); self.end_headers()
            self.wfile.write(b"Unexpected callback - no code or error.")

    def log_message(self, *args):
        pass  # suppress request logs to stdout


def _run_callback_server(server: HTTPServer, timeout: int) -> None:
    server.timeout = 1
    deadline = time.monotonic() + timeout
    while not getattr(server, "auth_code", None) is not None:
        server.handle_request()
        if time.monotonic() > deadline:
            break


# ---------------------------------------------------------------------------
# Main consent flow
# ---------------------------------------------------------------------------

def run_oauth() -> str:
    """Complete the consent flow and return a valid access token.

    Side-effect: writes WHOOP_REFRESH_TOKEN to .env.
    Raises RuntimeError on failure.
    """
    env = _load_env()
    client_id = env.get("WHOOP_CLIENT_ID") or os.environ.get("WHOOP_CLIENT_ID")
    client_secret = env.get("WHOOP_CLIENT_SECRET") or os.environ.get("WHOOP_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise RuntimeError(
            "WHOOP_CLIENT_ID / WHOOP_CLIENT_SECRET not found in .env. "
            "Add them before running the OAuth flow."
        )

    # --- idempotency check: test existing refresh token ---
    existing_refresh = env.get("WHOOP_REFRESH_TOKEN") or os.environ.get("WHOOP_REFRESH_TOKEN")
    if existing_refresh:
        print("WHOOP_REFRESH_TOKEN found in .env — testing it …", flush=True)
        result = try_refresh(client_id, client_secret, existing_refresh)
        if result:
            access, new_refresh = result
            if new_refresh != existing_refresh:
                _write_env_key("WHOOP_REFRESH_TOKEN", new_refresh)
                print("  Refresh token rotated — updated .env.")
            else:
                print("  Token valid — no rotation.")
            print("  Existing token OK. Consent flow skipped.")
            return access
        print("  Existing refresh token rejected — starting consent flow.")

    # --- generate state (CSRF protection; WHOOP requires >= 8 chars) ---
    state = secrets.token_urlsafe(16)

    # --- build authorization URL ---
    params = urllib.parse.urlencode({
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": _REDIRECT_URI,
        "scope": _SCOPES,
        "state": state,
    })
    auth_url = f"{_AUTH_URL}?{params}"

    # --- start local callback server ---
    server = HTTPServer(("127.0.0.1", _CALLBACK_PORT), _CallbackHandler)
    server.auth_code = None   # type: ignore[attr-defined]
    server.oauth_state = state  # type: ignore[attr-defined]

    t = threading.Thread(
        target=_run_callback_server, args=(server, _CONSENT_TIMEOUT_SEC), daemon=True
    )
    t.start()

    print(f"\nOpening WHOOP consent page …")
    print(f"  If it doesn't open automatically, visit:\n  {auth_url}\n")
    webbrowser.open(auth_url)

    # --- wait for code ---
    deadline = time.monotonic() + _CONSENT_TIMEOUT_SEC
    while server.auth_code is None and time.monotonic() < deadline:  # type: ignore[attr-defined]
        time.sleep(0.5)
    server.server_close()

    code = server.auth_code  # type: ignore[attr-defined]
    if not code:
        raise RuntimeError(
            f"No authorisation code received within {_CONSENT_TIMEOUT_SEC}s. "
            "Ensure the WHOOP app is registered with redirect URI "
            f"'{_REDIRECT_URI}' and try again."
        )

    print(f"Code received — exchanging for tokens …", flush=True)
    tokens = _exchange_code(code, client_id, client_secret)
    access_token = tokens["access_token"]
    refresh_token = tokens["refresh_token"]

    _write_env_key("WHOOP_REFRESH_TOKEN", refresh_token)
    print("Refresh token written to .env (gitignored).")
    print("OAuth flow complete.")
    return access_token


if __name__ == "__main__":
    try:
        token = run_oauth()
        print(f"\nAccess token obtained (first 12 chars): {token[:12]}…")
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
