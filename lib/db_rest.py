"""
httpx-based PostgREST client for cloud environments where psycopg2 and
supabase-py are unavailable (native module install blocked).

Auth flow:
    jwt = sign_in(SUPABASE_URL, ANON_KEY, email, password)
    with DbRest(SUPABASE_URL, ANON_KEY, jwt) as db:
        rows = db.select("media_inbox", filters={"status": "eq.pending"})

All requests hit PostgREST (/rest/v1/) with:
    apikey = anon_key   (identifies the client app)
    Authorization = Bearer <user JWT>   (identifies the authenticated user)

No service_role key is used here — the cloud Routine logs in as a normal user.
"""
from __future__ import annotations

from typing import Any

import httpx


def sign_in(supabase_url: str, anon_key: str, email: str, password: str) -> str:
    """Password-grant OAuth against Supabase Auth. Returns the JWT access_token."""
    r = httpx.post(
        f"{supabase_url.rstrip('/')}/auth/v1/token",
        params={"grant_type": "password"},
        headers={"apikey": anon_key, "Content-Type": "application/json"},
        json={"email": email, "password": password},
        timeout=30.0,
    )
    r.raise_for_status()
    return r.json()["access_token"]


class DbRest:
    """Thin wrapper around PostgREST REST API using httpx."""

    def __init__(self, supabase_url: str, anon_key: str, jwt: str) -> None:
        self._base = supabase_url.rstrip("/")
        self._anon_key = anon_key
        self._jwt = jwt
        self._client = httpx.Client(timeout=60.0)

    # ── internal ──────────────────────────────────────────────────────────────

    def _h(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        h: dict[str, str] = {
            "apikey": self._anon_key,
            "Authorization": f"Bearer {self._jwt}",
            "Content-Type": "application/json",
        }
        if extra:
            h.update(extra)
        return h

    # ── public API ────────────────────────────────────────────────────────────

    def select(
        self,
        table: str,
        *,
        select: str = "*",
        filters: dict[str, str] | None = None,
        limit: int | None = None,
        order: str | None = None,
    ) -> list[dict]:
        """GET /rest/v1/{table} with PostgREST filter params."""
        params: dict[str, Any] = {"select": select}
        if filters:
            params.update(filters)
        if limit is not None:
            params["limit"] = limit
        if order:
            params["order"] = order
        r = self._client.get(
            f"{self._base}/rest/v1/{table}",
            params=params,
            headers=self._h(),
        )
        r.raise_for_status()
        return r.json()

    def insert(self, table: str, row: dict, *, prefer_return: bool = True) -> dict | None:
        """POST /rest/v1/{table}. Returns the inserted row when prefer_return=True."""
        extra = {"Prefer": "return=representation"} if prefer_return else {}
        r = self._client.post(
            f"{self._base}/rest/v1/{table}",
            json=row,
            headers=self._h(extra),
        )
        r.raise_for_status()
        if prefer_return and r.content:
            rows = r.json()
            return rows[0] if rows else None
        return None

    def update(
        self,
        table: str,
        filters: dict[str, str],
        body: dict,
        *,
        prefer_return: bool = False,
    ) -> list[dict]:
        """PATCH /rest/v1/{table}?{filters}. Returns updated rows when prefer_return=True."""
        extra = {"Prefer": "return=representation"} if prefer_return else {}
        r = self._client.patch(
            f"{self._base}/rest/v1/{table}",
            params=filters,
            json=body,
            headers=self._h(extra),
        )
        r.raise_for_status()
        if prefer_return:
            return r.json() if r.content else []
        return []

    def rpc(self, func_name: str, args: dict) -> Any:
        """POST /rest/v1/rpc/{func_name} — call a PostgreSQL function via PostgREST."""
        r = self._client.post(
            f"{self._base}/rest/v1/rpc/{func_name}",
            json=args,
            headers=self._h(),
        )
        r.raise_for_status()
        return r.json() if r.content else None

    def claim_inbox_item(self, item_id: str) -> bool:
        """Atomically claim a media_inbox item (pending → processing).

        Uses a filtered PATCH so only one caller wins when two Routine fires
        race on the same item. Returns True if this call claimed it.
        """
        rows = self.update(
            "media_inbox",
            {"id": f"eq.{item_id}", "status": "eq.pending"},
            {"status": "processing"},
            prefer_return=True,
        )
        return len(rows) > 0

    # ── context manager ───────────────────────────────────────────────────────

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "DbRest":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()
