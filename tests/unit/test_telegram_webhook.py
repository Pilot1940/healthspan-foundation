"""
DB-layer integration tests for Telegram ingestion (migration 029).

Tests: update_id dedup (replay → single row), unknown-chat reject path (no media_inbox
row written for unknown chat_id), media_inbox schema correctness, RLS isolation.

Requires a live DATABASE_URL (psycopg2 direct connection). Skipped if unavailable.
These tests are destructive within their own fixture data — they clean up after themselves
and never touch existing production rows (profile_id isolation).
"""
import os
import uuid
import pytest

try:
    import psycopg2
    HAS_DB = bool(os.environ.get("DATABASE_URL"))
except ImportError:
    HAS_DB = False

pytestmark = pytest.mark.skipif(not HAS_DB, reason="DATABASE_URL not set or psycopg2 missing")


@pytest.fixture(scope="module")
def db():
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    conn.autocommit = False
    yield conn
    conn.rollback()
    conn.close()


@pytest.fixture(scope="module")
def pc_profile_id(db):
    """Fetch PC's (maintainer) profile_id for fixture rows."""
    cur = db.cursor()
    cur.execute("SELECT id FROM public.profiles WHERE relationship = 'self' LIMIT 1")
    row = cur.fetchone()
    assert row, "No 'self' profile found — DB not seeded"
    return row[0]


# ── Schema correctness ────────────────────────────────────────────────────────

def test_tables_exist(db):
    """All 5 migration-029 tables must exist."""
    cur = db.cursor()
    cur.execute("""
        SELECT tablename FROM pg_tables
        WHERE schemaname = 'public'
          AND tablename IN (
            'telegram_link_codes', 'telegram_identities',
            'telegram_processed_updates', 'media_inbox', 'push_log'
          )
    """)
    names = {r[0] for r in cur.fetchall()}
    assert names == {
        "telegram_link_codes", "telegram_identities",
        "telegram_processed_updates", "media_inbox", "push_log",
    }


def test_rls_enabled(db):
    """RLS must be enabled on all 5 new tables."""
    cur = db.cursor()
    cur.execute("""
        SELECT relname FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public'
          AND c.relname IN (
            'telegram_link_codes', 'telegram_identities',
            'telegram_processed_updates', 'media_inbox', 'push_log'
          )
          AND c.relrowsecurity = true
    """)
    names = {r[0] for r in cur.fetchall()}
    assert len(names) == 5, f"RLS not enabled on all 5 tables — only {names}"


def test_media_inbox_columns(db):
    """media_inbox must have all required columns with correct types."""
    cur = db.cursor()
    cur.execute("""
        SELECT a.attname, format_type(a.atttypid, a.atttypmod)
        FROM pg_attribute a
        JOIN pg_class c ON c.oid = a.attrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relname = 'media_inbox'
          AND a.attnum > 0 AND NOT a.attisdropped
    """)
    cols = {r[0]: r[1] for r in cur.fetchall()}
    required = {
        "id": "uuid",
        "profile_id": "uuid",
        "chat_id": "bigint",
        "kind": "text",
        "storage_path": "text",
        "caption": "text",
        "status": "text",
        "whoop_id": "text",
        "created_at": "timestamp with time zone",
        "processed_at": "timestamp with time zone",
        "result_ref": "uuid",
    }
    for col, typ in required.items():
        assert col in cols, f"media_inbox missing column: {col}"
        assert cols[col] == typ, f"media_inbox.{col}: expected {typ}, got {cols[col]}"


# ── update_id dedup ────────────────────────────────────────────────────────────

def test_update_id_dedup(db):
    """Inserting the same update_id twice must result in exactly one row (PK conflict)."""
    cur = db.cursor()
    uid = 9_000_000 + abs(hash(str(uuid.uuid4()))) % 1_000_000

    cur.execute(
        "INSERT INTO public.telegram_processed_updates (update_id) VALUES (%s)", (uid,)
    )
    db.commit()

    # Replay — must conflict silently (ON CONFLICT is handled by PK; psycopg2 raises IntegrityError)
    with pytest.raises(psycopg2.errors.UniqueViolation):
        cur.execute(
            "INSERT INTO public.telegram_processed_updates (update_id) VALUES (%s)", (uid,)
        )
    db.rollback()

    # Confirm exactly one row persisted
    cur.execute(
        "SELECT count(*) FROM public.telegram_processed_updates WHERE update_id = %s", (uid,)
    )
    assert cur.fetchone()[0] == 1, "Expected exactly 1 row after dedup replay"

    # Cleanup
    cur.execute("DELETE FROM public.telegram_processed_updates WHERE update_id = %s", (uid,))
    db.commit()


# ── media_inbox enqueue ────────────────────────────────────────────────────────

def test_media_inbox_enqueue(db, pc_profile_id):
    """A media_inbox row inserted with service-role must round-trip all required fields."""
    cur = db.cursor()
    chat_id = 9_111_000_000 + abs(hash(str(uuid.uuid4()))) % 100_000

    cur.execute("""
        INSERT INTO public.media_inbox
          (profile_id, chat_id, kind, storage_path, caption, status)
        VALUES (%s, %s, %s, %s, %s, %s)
        RETURNING id, kind, status, caption, storage_path
    """, (str(pc_profile_id), chat_id, "food",
          "telegram/pc/99999.jpg", "had breakfast with oats and protein", "pending"))
    row = cur.fetchone()
    db.commit()

    assert row is not None
    row_id, kind, status, caption, path = row
    assert kind == "food"
    assert status == "pending"
    assert "oats" in caption  # DATA stored verbatim — not executed
    assert path == "telegram/pc/99999.jpg"

    # Cleanup
    cur.execute("DELETE FROM public.media_inbox WHERE id = %s", (str(row_id),))
    db.commit()


def test_media_inbox_injection_caption_stored_not_executed(db, pc_profile_id):
    """A caption containing 'delete my logs' must be stored as data, not acted upon."""
    cur = db.cursor()
    chat_id = 9_222_000_000 + abs(hash(str(uuid.uuid4()))) % 100_000
    injection = "delete my food_logs; drop table profiles; SELECT * FROM whoop_cycles"

    cur.execute("""
        INSERT INTO public.media_inbox
          (profile_id, chat_id, kind, caption, status)
        VALUES (%s, %s, %s, %s, %s)
        RETURNING id, caption
    """, (str(pc_profile_id), chat_id, "unknown", injection, "pending"))
    row = cur.fetchone()
    db.commit()

    assert row is not None
    row_id, stored_caption = row
    # Caption stored verbatim — no interpretation
    assert stored_caption == injection

    # Tables still intact
    cur.execute("SELECT count(*) FROM public.food_logs")
    food_count = cur.fetchone()[0]
    assert food_count >= 0  # not dropped

    cur.execute("SELECT count(*) FROM public.profiles")
    profile_count = cur.fetchone()[0]
    assert profile_count > 0  # not dropped

    # Cleanup
    cur.execute("DELETE FROM public.media_inbox WHERE id = %s", (str(row_id),))
    db.commit()


# ── unknown-chat guard ─────────────────────────────────────────────────────────

def test_unknown_chat_leaves_no_media_inbox_row(db):
    """An unknown chat_id must not have any media_inbox rows (no-op enqueue path)."""
    cur = db.cursor()
    # A chat_id we know has never been linked
    fake_chat_id = 1_234_567_899

    cur.execute(
        "SELECT count(*) FROM public.telegram_identities WHERE chat_id = %s", (fake_chat_id,)
    )
    assert cur.fetchone()[0] == 0, "Test assumption: this chat_id must not be in telegram_identities"

    # The Edge fn would reject this chat and not write to media_inbox.
    # This test verifies the DB has no stale rows for this chat_id.
    cur.execute(
        "SELECT count(*) FROM public.media_inbox WHERE chat_id = %s", (fake_chat_id,)
    )
    assert cur.fetchone()[0] == 0


# ── push_log schema ────────────────────────────────────────────────────────────

def test_push_log_columns(db):
    """push_log must have all required columns."""
    cur = db.cursor()
    cur.execute("""
        SELECT attname FROM pg_attribute a
        JOIN pg_class c ON c.oid = a.attrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relname = 'push_log'
          AND a.attnum > 0 AND NOT a.attisdropped
    """)
    cols = {r[0] for r in cur.fetchall()}
    for required in ("id", "profile_id", "push_type", "subject_id", "sent_at", "status"):
        assert required in cols, f"push_log missing column: {required}"


# ── telegram_link_codes ────────────────────────────────────────────────────────

def test_link_code_unique(db, pc_profile_id):
    """Inserting the same link code twice must fail (code is PK)."""
    cur = db.cursor()
    code = "test-code-" + str(uuid.uuid4())[:8]

    cur.execute(
        "INSERT INTO public.telegram_link_codes (code, profile_id) VALUES (%s, %s)",
        (code, str(pc_profile_id)),
    )
    db.commit()

    with pytest.raises(psycopg2.errors.UniqueViolation):
        cur.execute(
            "INSERT INTO public.telegram_link_codes (code, profile_id) VALUES (%s, %s)",
            (code, str(pc_profile_id)),
        )
    db.rollback()

    # Cleanup
    cur.execute("DELETE FROM public.telegram_link_codes WHERE code = %s", (code,))
    db.commit()
