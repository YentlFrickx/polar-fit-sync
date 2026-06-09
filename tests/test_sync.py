# test_sync.py — unit tests for the sync orchestration layer.
#
# We use a fake PolarClient instead of respx because sync.py calls client
# methods as regular (not async) functions within an async function — the fake
# is simpler than a full httpx mock at this level.
#
# The temp SQLite DB and temp output dir are created fresh per test via pytest
# fixtures so tests are fully isolated.

import os
import pathlib
from datetime import datetime, timezone, timedelta
from typing import Optional
from unittest.mock import MagicMock

import pytest

from polar_fit_sync.db import Db
from polar_fit_sync.polar import Exercise, TokenExpiredError
from polar_fit_sync.sync import run_sync, RunResult


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def db(tmp_path):
    d = Db(str(tmp_path / "test.db"))
    d.init_schema()
    return d


@pytest.fixture
def output_dir(tmp_path):
    d = tmp_path / "fit"
    d.mkdir()
    return str(d)


def _make_exercise(eid: str, sport: str = "RUNNING", start_time: str = "2026-01-01T08:00:00Z") -> Exercise:
    return Exercise(
        id=eid,
        upload_time=None,
        start_time=start_time,
        sport=sport,
        duration=None,
        distance=None,
    )


def _make_token(db: Db, expires_in: int = 86400, offset_seconds: int = 0, status: str = "active"):
    """Store a token in the db. offset_seconds < 0 means issued in the past."""
    created_at = (
        datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)
    ).isoformat()
    db.set_token(
        access_token="tok",
        token_type="bearer",
        x_user_id="user1",
        member_id="m1",
        expires_in=expires_in,
        created_at=created_at,
    )
    if status != "active":
        db.set_token_status(status)


def _fake_client(exercises: list[Exercise], fit_content: bytes = b"FITDATA") -> MagicMock:
    client = MagicMock()
    client.list_exercises.return_value = exercises
    client.get_exercise.side_effect = lambda token, eid: next(
        (e for e in exercises if e.id == eid), exercises[0]
    )
    client.download_fit.return_value = fit_content
    return client


# ---------------------------------------------------------------------------
# Basic sync behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_first_run_downloads_all(db, output_dir):
    exercises = [_make_exercise("e1"), _make_exercise("e2")]
    _make_token(db)
    client = _fake_client(exercises)

    result = await run_sync(db, client, output_dir)

    assert result.status == "ok"
    assert result.new_files == 2
    assert result.errors == 0
    assert db.is_downloaded("e1")
    assert db.is_downloaded("e2")
    # Verify files exist on disk
    fit_files = list(pathlib.Path(output_dir).glob("*.fit"))
    assert len(fit_files) == 2


@pytest.mark.asyncio
async def test_second_run_downloads_zero(db, output_dir):
    exercises = [_make_exercise("e1")]
    _make_token(db)
    client = _fake_client(exercises)

    await run_sync(db, client, output_dir)
    result = await run_sync(db, client, output_dir)

    assert result.status == "ok"
    assert result.new_files == 0
    assert client.download_fit.call_count == 1  # called only on the first run


@pytest.mark.asyncio
async def test_no_token_returns_no_token(db, output_dir):
    client = _fake_client([])
    result = await run_sync(db, client, output_dir)

    assert result.status == "no_token"
    assert result.new_files == 0
    client.list_exercises.assert_not_called()


@pytest.mark.asyncio
async def test_partial_failure_records_successful_ones(db, output_dir):
    """One download failure should not prevent the others from being recorded."""
    exercises = [_make_exercise("e1"), _make_exercise("e2"), _make_exercise("e3")]
    _make_token(db)

    client = MagicMock()
    client.list_exercises.return_value = exercises

    def download_side_effect(token, eid):
        if eid == "e2":
            raise Exception("network error")
        return b"FITDATA"

    client.download_fit.side_effect = download_side_effect

    result = await run_sync(db, client, output_dir)

    assert result.status == "partial"
    assert result.new_files == 2
    assert result.errors == 1
    assert db.is_downloaded("e1")
    assert not db.is_downloaded("e2")  # failed — NOT recorded
    assert db.is_downloaded("e3")


# ---------------------------------------------------------------------------
# Token expiry (computed)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_computed_expiry_no_api_calls(db, output_dir):
    """An expired token should cause token_expired with zero API calls."""
    # Token issued 3601s ago with expires_in=3600 — clearly expired.
    _make_token(db, expires_in=3600, offset_seconds=-3601)
    client = _fake_client([])

    result = await run_sync(db, client, output_dir)

    assert result.status == "token_expired"
    client.list_exercises.assert_not_called()
    client.download_fit.assert_not_called()


@pytest.mark.asyncio
async def test_valid_token_not_flagged_as_expired(db, output_dir):
    """A token issued 10s ago with expires_in=86400 should be treated as active."""
    _make_token(db, expires_in=86400, offset_seconds=-10)
    client = _fake_client([])

    result = await run_sync(db, client, output_dir)

    assert result.status == "ok"


# ---------------------------------------------------------------------------
# Pre-flagged expired token
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_preflagged_token_expired_no_api_calls(db, output_dir):
    """A token with status=token_expired should not trigger any API calls."""
    _make_token(db, expires_in=86400, status="token_expired")
    client = _fake_client([])

    result = await run_sync(db, client, output_dir)

    assert result.status == "token_expired"
    client.list_exercises.assert_not_called()


# ---------------------------------------------------------------------------
# 401 mid-run flips token status
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_401_mid_run_flips_token_status(db, output_dir):
    _make_token(db)
    client = MagicMock()
    client.list_exercises.side_effect = TokenExpiredError("401")

    result = await run_sync(db, client, output_dir)

    assert result.status == "token_expired"
    token = db.get_token()
    assert token is not None
    assert token.status == "token_expired"


@pytest.mark.asyncio
async def test_401_during_download_flips_token_status(db, output_dir):
    exercises = [_make_exercise("e1")]
    _make_token(db)
    client = MagicMock()
    client.list_exercises.return_value = exercises
    client.download_fit.side_effect = TokenExpiredError("401")

    result = await run_sync(db, client, output_dir)

    assert result.status == "token_expired"
    token = db.get_token()
    assert token is not None
    assert token.status == "token_expired"


# ---------------------------------------------------------------------------
# Atomic write
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_atomic_write_no_tmp_left_after_success(db, output_dir):
    exercises = [_make_exercise("e1")]
    _make_token(db)
    client = _fake_client(exercises)

    await run_sync(db, client, output_dir)

    # The atomic-write helper names temp files as {name}.fit.tmp (appended, not
    # replacing the .fit extension), so we check the *.fit.tmp pattern.
    tmp_files = list(pathlib.Path(output_dir).glob("*.fit.tmp"))
    assert tmp_files == [], "No .fit.tmp files should remain after a successful write"


@pytest.mark.asyncio
async def test_failed_write_does_not_record_exercise(db, output_dir, monkeypatch):
    """If rename fails, the exercise should NOT be recorded in the DB."""
    exercises = [_make_exercise("e1")]
    _make_token(db)
    client = _fake_client(exercises)

    # Simulate os.rename raising after the .tmp file has been written.
    original_rename = os.rename

    def broken_rename(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr("polar_fit_sync.sync.os.rename", broken_rename)

    result = await run_sync(db, client, output_dir)

    assert result.errors == 1
    assert not db.is_downloaded("e1")


# ---------------------------------------------------------------------------
# Targeted (webhook) sync
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_targeted_sync_fetches_single_exercise(db, output_dir):
    exercises = [_make_exercise("e1"), _make_exercise("e2")]
    _make_token(db)
    client = _fake_client(exercises)

    result = await run_sync(db, client, output_dir, target_id="e1", trigger="webhook")

    assert result.status == "ok"
    assert result.new_files == 1
    client.get_exercise.assert_called_once_with("tok", "e1")
    # e2 was not requested
    assert not db.is_downloaded("e2")


@pytest.mark.asyncio
async def test_targeted_sync_falls_back_to_full_list(db, output_dir):
    """If get_exercise raises, the sync should fall back to list_exercises."""
    _make_token(db)
    client = MagicMock()
    client.get_exercise.side_effect = Exception("not found")
    client.list_exercises.return_value = [_make_exercise("e1")]
    client.download_fit.return_value = b"FITDATA"

    result = await run_sync(db, client, output_dir, target_id="e1", trigger="webhook")

    assert result.status == "ok"
    client.list_exercises.assert_called_once()


# ---------------------------------------------------------------------------
# Trigger is recorded
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_trigger_recorded_in_run(db, output_dir):
    _make_token(db)
    client = _fake_client([])

    await run_sync(db, client, output_dir, trigger="webhook")

    last = db.last_run()
    assert last is not None
    assert last["trigger"] == "webhook"


# ---------------------------------------------------------------------------
# Sport-type filtering
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_include_filter_keeps_only_listed_sports(db, output_dir):
    exercises = [
        _make_exercise("e1", sport="RUNNING"),
        _make_exercise("e2", sport="CYCLING"),
        _make_exercise("e3", sport="SWIMMING"),
    ]
    _make_token(db)
    client = _fake_client(exercises)
    result = await run_sync(db, client, output_dir, sport_filter=frozenset({"RUNNING", "CYCLING"}), filter_mode="include")
    assert result.status == "ok"
    assert result.new_files == 2
    assert result.errors == 0
    assert db.is_downloaded("e1")
    assert db.is_downloaded("e2")
    assert not db.is_downloaded("e3")


@pytest.mark.asyncio
async def test_exclude_filter_drops_listed_sports(db, output_dir):
    exercises = [
        _make_exercise("e1", sport="RUNNING"),
        _make_exercise("e2", sport="CYCLING"),
        _make_exercise("e3", sport="SWIMMING"),
    ]
    _make_token(db)
    client = _fake_client(exercises)
    result = await run_sync(db, client, output_dir, sport_filter=frozenset({"SWIMMING"}), filter_mode="exclude")
    assert result.status == "ok"
    assert result.new_files == 2
    assert result.errors == 0
    assert db.is_downloaded("e1")
    assert db.is_downloaded("e2")
    assert not db.is_downloaded("e3")


@pytest.mark.asyncio
async def test_empty_filter_downloads_all(db, output_dir):
    exercises = [
        _make_exercise("e1", sport="RUNNING"),
        _make_exercise("e2", sport="SWIMMING"),
        _make_exercise("e3", sport="YOGA"),
    ]
    _make_token(db)
    client = _fake_client(exercises)
    result = await run_sync(db, client, output_dir, sport_filter=frozenset(), filter_mode="include")
    assert result.status == "ok"
    assert result.new_files == 3
    assert db.is_downloaded("e1")
    assert db.is_downloaded("e2")
    assert db.is_downloaded("e3")


@pytest.mark.asyncio
async def test_filter_case_insensitive(db, output_dir):
    exercises = [
        _make_exercise("e1", sport="RUNNING"),
        _make_exercise("e2", sport="CYCLING"),
    ]
    _make_token(db)
    client = _fake_client(exercises)
    result = await run_sync(db, client, output_dir, sport_filter=frozenset({"RUNNING"}), filter_mode="include")
    assert result.status == "ok"
    assert result.new_files == 1
    assert db.is_downloaded("e1")
    assert not db.is_downloaded("e2")


@pytest.mark.asyncio
async def test_include_filter_drops_null_sport(db, output_dir):
    exercises = [
        _make_exercise("e1", sport="RUNNING"),
        _make_exercise("e2", sport=None),
    ]
    _make_token(db)
    client = _fake_client(exercises)
    result = await run_sync(db, client, output_dir, sport_filter=frozenset({"RUNNING"}), filter_mode="include")
    assert result.status == "ok"
    assert result.new_files == 1
    assert db.is_downloaded("e1")
    assert not db.is_downloaded("e2")


@pytest.mark.asyncio
async def test_exclude_filter_keeps_null_sport(db, output_dir):
    exercises = [
        _make_exercise("e1", sport="SWIMMING"),
        _make_exercise("e2", sport=None),
    ]
    _make_token(db)
    client = _fake_client(exercises)
    result = await run_sync(db, client, output_dir, sport_filter=frozenset({"SWIMMING"}), filter_mode="exclude")
    assert result.status == "ok"
    assert result.new_files == 1
    assert not db.is_downloaded("e1")
    assert db.is_downloaded("e2")


@pytest.mark.asyncio
async def test_targeted_sync_respects_filter(db, output_dir):
    exercises = [_make_exercise("e1", sport="SWIMMING")]
    _make_token(db)
    client = _fake_client(exercises)
    result = await run_sync(
        db, client, output_dir,
        target_id="e1",
        trigger="webhook",
        sport_filter=frozenset({"RUNNING"}),
        filter_mode="include",
    )
    assert result.status == "ok"
    assert result.new_files == 0
    assert result.errors == 0
    assert not db.is_downloaded("e1")
    client.download_fit.assert_not_called()


@pytest.mark.asyncio
async def test_filtered_out_not_counted_as_error(db, output_dir):
    exercises = [
        _make_exercise("e1", sport="RUNNING"),
        _make_exercise("e2", sport="SWIMMING"),
        _make_exercise("e3", sport="CYCLING"),
    ]
    _make_token(db)
    client = _fake_client(exercises)
    result = await run_sync(db, client, output_dir, sport_filter=frozenset({"RUNNING", "CYCLING"}), filter_mode="include")
    assert result.status == "ok"
    assert result.new_files == 2
    assert result.errors == 0
    assert db.is_downloaded("e1")
    assert not db.is_downloaded("e2")
    assert db.is_downloaded("e3")
