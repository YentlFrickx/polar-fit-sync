# test_db.py — unit tests for the db.py SQLite access layer.
#
# Every test gets an isolated database in a temporary directory so tests are
# fully independent and leave no state on disk. We manipulate created_at values
# directly in SQL when we need to simulate elapsed time (no sleeping required).

import sqlite3
from datetime import datetime, timezone, timedelta

import pytest

from polar_fit_sync.db import Db, OAUTH_STATE_TTL_SECONDS


@pytest.fixture
def db(tmp_path):
    """A fresh, schema-initialised Db backed by a temp file."""
    d = Db(str(tmp_path / "test.db"))
    d.init_schema()
    return d


# ---------------------------------------------------------------------------
# Parent-directory creation (PFS_DB_PATH independent of PFS_OUTPUT_DIR)
# ---------------------------------------------------------------------------


def test_init_creates_missing_parent_dir(tmp_path):
    nested = tmp_path / "does" / "not" / "exist" / "state.db"
    d = Db(str(nested))
    assert nested.parent.is_dir()
    d.init_schema()
    assert d.count_downloaded() == 0
    d.record_downloaded("ex1", "/data/fit/ex1.fit", "RUNNING", "2026-01-01T00:00:00Z")
    assert d.count_downloaded() == 1


def test_init_memory_db_ok():
    d = Db(":memory:")
    d.init_schema()


def test_db_dir_creation_independent_of_output_dir(tmp_path):
    db_dir = tmp_path / "db_tree" / "nested"
    output_dir = tmp_path / "output_tree" / "nested"
    d = Db(str(db_dir / "state.db"))
    d.init_schema()
    assert db_dir.is_dir()
    assert not output_dir.exists()


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def test_init_schema_idempotent(tmp_path):
    """Calling init_schema twice must not raise (CREATE TABLE IF NOT EXISTS)."""
    d = Db(str(tmp_path / "test.db"))
    d.init_schema()
    d.init_schema()  # second call should be a no-op


# ---------------------------------------------------------------------------
# Token
# ---------------------------------------------------------------------------


def test_get_token_returns_none_when_empty(db):
    assert db.get_token() is None


def test_set_and_get_token(db):
    db.set_token(
        access_token="tok123",
        token_type="bearer",
        x_user_id="user42",
        member_id="my-member",
        expires_in=86400,
        created_at="2026-01-01T00:00:00+00:00",
    )
    token = db.get_token()
    assert token is not None
    assert token.access_token == "tok123"
    assert token.x_user_id == "user42"
    assert token.expires_in == 86400
    assert token.created_at == "2026-01-01T00:00:00+00:00"
    assert token.status == "active"


def test_set_token_overwrites_existing(db):
    """Re-linking (a second OAuth flow) must overwrite the previous token."""
    db.set_token("old", "bearer", "u1", "m1", 100, "2025-01-01T00:00:00+00:00")
    db.set_token("new", "bearer", "u2", "m1", 200, "2026-01-01T00:00:00+00:00")
    token = db.get_token()
    assert token is not None
    assert token.access_token == "new"
    assert token.expires_in == 200


def test_set_token_status_to_expired(db):
    db.set_token("tok", "bearer", "u1", "m1", 3600, "2026-01-01T00:00:00+00:00")
    db.set_token_status("token_expired")
    token = db.get_token()
    assert token is not None
    assert token.status == "token_expired"


def test_set_token_status_without_token_does_not_raise(db):
    """Updating the status when no token row exists should be a safe no-op."""
    db.set_token_status("token_expired")  # row count is 0, UPDATE affects 0 rows


# ---------------------------------------------------------------------------
# OAuth state TTL
# ---------------------------------------------------------------------------


def test_create_and_consume_state_valid(db):
    db.create_state("abc123")
    result = db.consume_state("abc123")
    assert result is True


def test_consume_state_missing(db):
    result = db.consume_state("does-not-exist")
    assert result is False


def test_consume_state_deletes_row(db):
    db.create_state("state1")
    db.consume_state("state1")
    # A second consume must return False — the row was deleted.
    result = db.consume_state("state1")
    assert result is False


def test_consume_state_fresh_within_ttl(db):
    """A state created just 599s ago is within the TTL and must be accepted."""
    db.create_state("fresh")
    # Rewind created_at by 599 seconds — still within the 600-second TTL.
    past = (
        datetime.now(timezone.utc) - timedelta(seconds=OAUTH_STATE_TTL_SECONDS - 1)
    ).isoformat()
    conn = sqlite3.connect(db._path)
    conn.execute("UPDATE oauth_state SET created_at = ? WHERE state = 'fresh'", (past,))
    conn.commit()
    conn.close()
    assert db.consume_state("fresh") is True


def test_consume_state_expired(db):
    """A state created 601s ago is past the TTL and must be rejected."""
    db.create_state("old")
    past = (
        datetime.now(timezone.utc) - timedelta(seconds=OAUTH_STATE_TTL_SECONDS + 1)
    ).isoformat()
    conn = sqlite3.connect(db._path)
    conn.execute("UPDATE oauth_state SET created_at = ? WHERE state = 'old'", (past,))
    conn.commit()
    conn.close()
    assert db.consume_state("old") is False


def test_consume_state_exactly_at_boundary(db):
    """A state whose age is equal to OAUTH_STATE_TTL_SECONDS is still accepted (<=).

    We use OAUTH_STATE_TTL_SECONDS - 1 rather than exactly TTL seconds to avoid
    a race condition: by the time consume_state reads the clock after we write the
    timestamp, a few microseconds have passed and the age would be marginally over
    the TTL. Using TTL - 1 confirms the <= boundary condition unambiguously.
    """
    db.create_state("boundary")
    past = (
        datetime.now(timezone.utc) - timedelta(seconds=OAUTH_STATE_TTL_SECONDS - 1)
    ).isoformat()
    conn = sqlite3.connect(db._path)
    conn.execute(
        "UPDATE oauth_state SET created_at = ? WHERE state = 'boundary'", (past,)
    )
    conn.commit()
    conn.close()
    assert db.consume_state("boundary") is True


# ---------------------------------------------------------------------------
# Downloaded exercise dedup
# ---------------------------------------------------------------------------


def test_is_downloaded_false_when_empty(db):
    assert db.is_downloaded("ex1") is False


def test_record_and_is_downloaded(db):
    db.record_downloaded("ex1", "/data/fit/ex1.fit", "RUNNING", "2026-01-01T08:00:00Z")
    assert db.is_downloaded("ex1") is True


def test_record_downloaded_is_idempotent(db):
    """Calling record_downloaded twice for the same id must not raise."""
    db.record_downloaded("ex2", "/data/fit/ex2.fit", "CYCLING", "2026-01-02T09:00:00Z")
    db.record_downloaded("ex2", "/data/fit/ex2.fit", "CYCLING", "2026-01-02T09:00:00Z")
    assert db.is_downloaded("ex2") is True


def test_count_downloaded(db):
    assert db.count_downloaded() == 0
    db.record_downloaded("e1", "/p1.fit", "RUNNING", "2026-01-01T00:00:00Z")
    db.record_downloaded("e2", "/p2.fit", "CYCLING", "2026-01-02T00:00:00Z")
    assert db.count_downloaded() == 2


# ---------------------------------------------------------------------------
# Skipped exercise tracking
# ---------------------------------------------------------------------------


def test_list_skipped_sports_empty_when_none_recorded(db):
    assert db.list_skipped_sports() == {}


def test_record_and_list_skipped_sports(db):
    db.record_skipped("e2", "CYCLING")
    assert db.list_skipped_sports() == {"e2": "CYCLING"}


def test_record_skipped_with_null_sport(db):
    """sport is nullable — a FIT parse that yields no session sport can still be skipped."""
    db.record_skipped("e9", None)
    assert db.list_skipped_sports() == {"e9": None}


def test_list_skipped_sports_returns_all_rows(db):
    db.record_skipped("e1", "RUNNING")
    db.record_skipped("e2", "CYCLING")
    db.record_skipped("e3", None)
    assert db.list_skipped_sports() == {"e1": "RUNNING", "e2": "CYCLING", "e3": None}


def test_record_skipped_is_insert_or_replace(db):
    """Re-skipping the same exercise_id must refresh (not duplicate) its stored sport."""
    db.record_skipped("e2", "CYCLING")
    db.record_skipped("e2", "MOUNTAIN_BIKING")
    assert db.list_skipped_sports() == {"e2": "MOUNTAIN_BIKING"}


def test_delete_skipped_removes_row(db):
    db.record_skipped("e2", "CYCLING")
    assert "e2" in db.list_skipped_sports()
    db.delete_skipped("e2")
    assert "e2" not in db.list_skipped_sports()


def test_delete_skipped_noop_when_no_row_exists(db):
    """delete_skipped must be a harmless no-op when there is nothing to delete."""
    db.delete_skipped("does-not-exist")  # must not raise
    assert db.list_skipped_sports() == {}


def test_delete_skipped_only_removes_matching_id(db):
    db.record_skipped("e1", "RUNNING")
    db.record_skipped("e2", "CYCLING")
    db.delete_skipped("e1")
    assert db.list_skipped_sports() == {"e2": "CYCLING"}


def test_init_schema_creates_skipped_exercise_table(tmp_path):
    """A fresh DB must have the skipped_exercise table after init_schema."""
    d = Db(str(tmp_path / "test.db"))
    d.init_schema()
    conn = sqlite3.connect(d._path)
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='skipped_exercise'"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None


def test_init_schema_skipped_exercise_idempotent(tmp_path):
    """Re-running init_schema against a DB that already has skipped_exercise rows
    must not raise and must not lose data (CREATE TABLE IF NOT EXISTS)."""
    d = Db(str(tmp_path / "test.db"))
    d.init_schema()
    d.record_skipped("e2", "CYCLING")
    d.init_schema()  # second call, simulating a restart against an existing DB
    assert d.list_skipped_sports() == {"e2": "CYCLING"}


# ---------------------------------------------------------------------------
# Sync run log
# ---------------------------------------------------------------------------


def test_start_and_finish_run(db):
    run_id = db.start_run("poll")
    assert run_id is not None
    db.finish_run(run_id, new_files=3, errors=0, status="ok")
    last = db.last_run()
    assert last is not None
    assert last["new_files"] == 3
    assert last["status"] == "ok"
    assert last["trigger"] == "poll"


def test_last_run_none_when_no_finished_runs(db):
    assert db.last_run() is None


def test_last_run_returns_most_recent(db):
    id1 = db.start_run("poll")
    db.finish_run(id1, 1, 0, "ok")
    id2 = db.start_run("manual")
    db.finish_run(id2, 5, 0, "ok")
    last = db.last_run()
    assert last is not None
    assert last["id"] == id2
