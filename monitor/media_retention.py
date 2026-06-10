"""health-media retention prune (BACKLOG #7).

Photos pile up in the private `health-media` Storage bucket forever. Once a
`media_inbox` row is terminal (done/failed) the source image has served its
purpose; after `media.retention_days` (system_config, seeded 45 by mig 062)
the Storage object is deleted and `media_inbox.storage_path` is nulled — the
DB row itself is KEPT for audit. Rows still pending/processing/staged (and
anything younger than the window) are never touched.

Runs as service_role (Storage delete needs it; the bucket is private with a
drainer-only SELECT policy — mig 053). Per-object best-effort: one failed
delete doesn't stop the sweep, but any hard failure exits non-zero so the
GH Actions cron (media-retention.yml) surfaces it.

CLI: python -m monitor.media_retention [--dry-run]
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)

_BUCKET = "health-media"
_DEFAULT_RETENTION_DAYS = 45  # fallback only; the real value is system_config media.retention_days
_TERMINAL_STATUSES = "(done,failed)"


def _svc_request(url: str, key: str, path: str, *, method: str = "GET",
                 body: dict | None = None) -> tuple[int, str]:
    """One service-role REST call. Returns (status, body_text); HTTP errors are
    returned, not raised, so callers can branch on 404 etc."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url.rstrip("/") + path, data=data, method=method)
    req.add_header("Authorization", f"Bearer {key}")
    req.add_header("apikey", key)
    if body is not None:  # Content-Type with an empty body 400s on the Storage API
        req.add_header("Content-Type", "application/json")
    if method == "PATCH":
        req.add_header("Prefer", "return=minimal")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")


def _retention_days(url: str, key: str) -> int:
    status, body = _svc_request(
        url, key, "/rest/v1/system_config?key=eq.media.retention_days&select=value,is_active")
    if status == 200:
        rows = json.loads(body)
        if rows and rows[0].get("is_active"):
            try:
                return int(rows[0]["value"])
            except (TypeError, ValueError):
                pass
    log.warning("media.retention_days not readable (HTTP %s) — falling back to %s",
                status, _DEFAULT_RETENTION_DAYS)
    return _DEFAULT_RETENTION_DAYS


def prune(url: str, key: str, *, dry_run: bool = False) -> dict:
    """Delete expired health-media objects; null storage_path on their rows.
    Returns {"candidates": n, "pruned": n, "missing": n, "failed": n}."""
    days = _retention_days(url, key)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    status, body = _svc_request(
        url, key,
        "/rest/v1/media_inbox?select=id,storage_path"
        f"&status=in.{_TERMINAL_STATUSES}"
        "&storage_path=not.is.null"
        f"&created_at=lt.{urllib.parse.quote(cutoff)}",
    )
    if status != 200:
        raise RuntimeError(f"media_inbox list failed: HTTP {status}: {body[:200]}")
    rows = json.loads(body)
    out = {"retention_days": days, "candidates": len(rows),
           "pruned": 0, "missing": 0, "failed": 0}

    for row in rows:
        path = (row.get("storage_path") or "").lstrip("/")
        # storage_path may be stored as "<bucket>/<object>" or bare "<object>"
        object_path = path[len(_BUCKET) + 1:] if path.startswith(_BUCKET + "/") else path
        if dry_run:
            print(f"  would prune {row['id']}: {path}")
            continue
        s, b = _svc_request(url, key,
                            f"/storage/v1/object/{_BUCKET}/{urllib.parse.quote(object_path)}",
                            method="DELETE")
        if s in (200, 204):
            out["pruned"] += 1
        elif s in (400, 404) and ("not_found" in b or "Not found" in b or s == 404):
            out["missing"] += 1  # already gone — still null the path below
        else:
            out["failed"] += 1
            log.error("storage delete failed for %s (%s): HTTP %s %s",
                      row["id"], path, s, b[:200])
            continue
        s2, b2 = _svc_request(url, key, f"/rest/v1/media_inbox?id=eq.{row['id']}",
                              method="PATCH", body={"storage_path": None})
        if s2 not in (200, 204):
            out["failed"] += 1
            log.error("storage_path null-out failed for %s: HTTP %s %s", row["id"], s2, b2[:200])
    return out


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Prune expired health-media Storage objects.")
    parser.add_argument("--dry-run", action="store_true",
                        help="List candidates without deleting anything")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    url = os.environ["SUPABASE_URL"]
    key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    out = prune(url, key, dry_run=args.dry_run)
    print(f"media retention: {out}")
    if out["failed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
