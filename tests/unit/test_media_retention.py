"""Unit tests for monitor/media_retention.py (BACKLOG #7). REST mocked."""
from __future__ import annotations

import json
from unittest.mock import patch

from monitor import media_retention as mr

URL, KEY = "https://x.supabase.co", "svc-key"


def _cfg_ok(days=45):
    return (200, json.dumps([{"value": days, "is_active": True}]))


def _rows(*paths):
    return (200, json.dumps([{"id": f"id-{i}", "storage_path": p}
                             for i, p in enumerate(paths)]))


class TestPrune:
    def test_deletes_object_and_nulls_path(self):
        calls = []

        def fake(url, key, path, *, method="GET", body=None):
            calls.append((method, path, body))
            if "system_config" in path:
                return _cfg_ok()
            if path.startswith("/rest/v1/media_inbox?select"):
                return _rows("telegram/u1/a.jpg")
            if method == "DELETE":
                return (200, '{"message":"Successfully deleted"}')
            if method == "PATCH":
                return (204, "")
            raise AssertionError(f"unexpected call {method} {path}")

        with patch.object(mr, "_svc_request", side_effect=fake):
            out = mr.prune(URL, KEY)
        assert out["pruned"] == 1 and out["failed"] == 0
        deletes = [c for c in calls if c[0] == "DELETE"]
        assert deletes == [("DELETE", "/storage/v1/object/health-media/telegram/u1/a.jpg", None)]
        patches = [c for c in calls if c[0] == "PATCH"]
        assert patches[0][2] == {"storage_path": None}

    def test_missing_object_still_nulls_path(self):
        # Storage answers HTTP 400 with a not_found body for an already-gone object.
        def fake(url, key, path, *, method="GET", body=None):
            if "system_config" in path:
                return _cfg_ok()
            if path.startswith("/rest/v1/media_inbox?select"):
                return _rows("telegram/u1/gone.jpg")
            if method == "DELETE":
                return (400, '{"statusCode":"404","error":"not_found","message":"Object not found"}')
            if method == "PATCH":
                return (204, "")
            raise AssertionError(path)

        with patch.object(mr, "_svc_request", side_effect=fake):
            out = mr.prune(URL, KEY)
        assert out["missing"] == 1 and out["pruned"] == 0 and out["failed"] == 0

    def test_failed_delete_skips_patch_and_counts(self):
        patched = []

        def fake(url, key, path, *, method="GET", body=None):
            if "system_config" in path:
                return _cfg_ok()
            if path.startswith("/rest/v1/media_inbox?select"):
                return _rows("telegram/u1/err.jpg")
            if method == "DELETE":
                return (500, "boom")
            if method == "PATCH":
                patched.append(path)
                return (204, "")
            raise AssertionError(path)

        with patch.object(mr, "_svc_request", side_effect=fake):
            out = mr.prune(URL, KEY)
        assert out["failed"] == 1 and out["pruned"] == 0
        assert patched == []          # storage_path kept so the next run retries

    def test_dry_run_deletes_nothing(self):
        mutations = []

        def fake(url, key, path, *, method="GET", body=None):
            if method in ("DELETE", "PATCH"):
                mutations.append(path)
            if "system_config" in path:
                return _cfg_ok()
            if path.startswith("/rest/v1/media_inbox?select"):
                return _rows("telegram/u1/a.jpg", "telegram/u1/b.jpg")
            return (200, "[]")

        with patch.object(mr, "_svc_request", side_effect=fake):
            out = mr.prune(URL, KEY, dry_run=True)
        assert out["candidates"] == 2 and mutations == []

    def test_bucket_prefixed_storage_path_is_stripped(self):
        deletes = []

        def fake(url, key, path, *, method="GET", body=None):
            if "system_config" in path:
                return _cfg_ok()
            if path.startswith("/rest/v1/media_inbox?select"):
                return _rows("health-media/telegram/u1/a.jpg")
            if method == "DELETE":
                deletes.append(path)
                return (200, "{}")
            return (204, "")

        with patch.object(mr, "_svc_request", side_effect=fake):
            mr.prune(URL, KEY)
        assert deletes == ["/storage/v1/object/health-media/telegram/u1/a.jpg"]

    def test_retention_days_falls_back_when_key_unreadable(self):
        def fake(url, key, path, *, method="GET", body=None):
            if "system_config" in path:
                return (500, "boom")
            if path.startswith("/rest/v1/media_inbox?select"):
                return (200, "[]")
            raise AssertionError(path)

        with patch.object(mr, "_svc_request", side_effect=fake):
            out = mr.prune(URL, KEY)
        assert out["retention_days"] == mr._DEFAULT_RETENTION_DAYS
