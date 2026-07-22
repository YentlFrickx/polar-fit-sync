# test_scheduler.py — unit tests for the APScheduler wiring.
#
# We verify that build_scheduler produces a scheduler with the correct number
# of jobs and the configured interval, without actually starting the scheduler.
# We also verify that the registered job is an async callable, matching the
# _sync_runner defined in web.py (AsyncIOScheduler natively awaits async jobs).

import inspect
from datetime import datetime, timedelta, timezone

import pytest

from polar_fit_sync.config import Settings
from polar_fit_sync.scheduler import build_scheduler


def _settings(sync_mode: str, interval: int = 30) -> Settings:
    kwargs = {
        "polar_client_id": "c",
        "polar_client_secret": "s",
        "polar_redirect_uri": "http://localhost/cb",
        "pfs_sync_mode": sync_mode,
        "pfs_sync_interval_minutes": interval,
        "pfs_db_path": "/tmp/test.db",
        "pfs_output_dir": "/tmp/fit",
    }
    if sync_mode in ("webhook", "both"):
        kwargs["pfs_webhook_secret"] = "secret"
    return Settings(**kwargs)


def _noop():
    pass


async def _async_noop():
    pass


def test_poll_mode_has_one_job():
    scheduler = build_scheduler(_settings("poll"), _noop)
    jobs = scheduler.get_jobs()
    assert len(jobs) == 1


def test_both_mode_has_one_job():
    scheduler = build_scheduler(_settings("both"), _noop)
    jobs = scheduler.get_jobs()
    assert len(jobs) == 1


def test_webhook_mode_has_no_jobs():
    scheduler = build_scheduler(_settings("webhook"), _noop)
    jobs = scheduler.get_jobs()
    assert len(jobs) == 0


def test_poll_job_interval_matches_settings():
    scheduler = build_scheduler(_settings("poll", interval=45), _noop)
    job = scheduler.get_jobs()[0]
    # APScheduler stores the interval in the trigger's fields.
    # The trigger type is IntervalTrigger; we check the interval attribute.
    trigger = job.trigger
    # trigger.interval is a timedelta
    assert trigger.interval.total_seconds() == 45 * 60


def test_both_mode_interval_matches_settings():
    scheduler = build_scheduler(_settings("both", interval=15), _noop)
    job = scheduler.get_jobs()[0]
    assert job.trigger.interval.total_seconds() == 15 * 60


def test_poll_job_id_is_polar_sync():
    scheduler = build_scheduler(_settings("poll"), _noop)
    job = scheduler.get_jobs()[0]
    assert job.id == "polar_sync"


def test_poll_mode_job_is_async_callable():
    # AsyncIOScheduler awaits async job functions directly, so the registered
    # runner must be a coroutine function. A sync wrapper that calls
    # asyncio.ensure_future() would silently swallow exceptions — async is
    # required for APScheduler to surface errors through its error handling.
    scheduler = build_scheduler(_settings("poll"), _async_noop)
    job = scheduler.get_jobs()[0]
    assert inspect.iscoroutinefunction(job.func), (
        "The poll-mode job must be an async callable so AsyncIOScheduler can "
        "await it and surface exceptions properly."
    )


def test_both_mode_job_is_async_callable():
    # Same requirement holds in 'both' mode — the same _sync_runner is used.
    scheduler = build_scheduler(_settings("both"), _async_noop)
    job = scheduler.get_jobs()[0]
    assert inspect.iscoroutinefunction(job.func), (
        "The both-mode job must be an async callable so AsyncIOScheduler can "
        "await it and surface exceptions properly."
    )


def _settings_with_startup(
    sync_mode: str, interval: int = 30, pfs_sync_on_startup: bool = True
) -> Settings:
    """Like the existing _settings() helper in this file, but exposes
    pfs_sync_on_startup. Kept separate so existing tests/helpers are untouched."""
    kwargs = {
        "polar_client_id": "c",
        "polar_client_secret": "s",
        "polar_redirect_uri": "http://localhost/cb",
        "pfs_sync_mode": sync_mode,
        "pfs_sync_interval_minutes": interval,
        "pfs_db_path": "/tmp/test.db",
        "pfs_output_dir": "/tmp/fit",
        "pfs_sync_on_startup": pfs_sync_on_startup,
    }
    if sync_mode in ("webhook", "both"):
        kwargs["pfs_webhook_secret"] = "secret"
    return Settings(**kwargs)


def test_startup_enabled_poll_next_run_time_is_now():
    """FR2: pfs_sync_on_startup=True + poll -> the polar_sync job's
    next_run_time is set to (approximately) now, not None, and not deferred
    to a full interval away."""
    before = datetime.now(timezone.utc)
    scheduler = build_scheduler(
        _settings_with_startup("poll", pfs_sync_on_startup=True), _noop
    )
    job = scheduler.get_jobs()[0]
    after = datetime.now(timezone.utc)

    assert job.next_run_time is not None
    assert before - timedelta(seconds=5) <= job.next_run_time <= after + timedelta(seconds=5)


def test_startup_enabled_both_next_run_time_is_now():
    """Same acceleration must hold in 'both' mode (FR2, scenario 2)."""
    before = datetime.now(timezone.utc)
    scheduler = build_scheduler(
        _settings_with_startup("both", pfs_sync_on_startup=True), _noop
    )
    job = scheduler.get_jobs()[0]
    after = datetime.now(timezone.utc)

    assert job.next_run_time is not None
    assert before - timedelta(seconds=5) <= job.next_run_time <= after + timedelta(seconds=5)


def test_startup_disabled_poll_job_has_no_accelerated_next_run_time():
    """FR6: pfs_sync_on_startup=False -> the next_run_time kwarg passed to
    add_job is OMITTED (never None). Before the scheduler is started,
    APScheduler's add_job() defers computing next_run_time entirely (per its
    own docstring: values defaulting to `undefined` are "replaced ... when
    the job is scheduled, which happens when the scheduler is started"), so
    the Job object simply has no next_run_time attribute yet — this is
    pre-existing APScheduler behaviour, not something this feature changes.
    We assert that directly (guards against a future accidental `None`
    default, which WOULD show up as an explicit attribute) and then start
    the scheduler to force computation, confirming the real first fire is
    the trigger's normal now+interval, not accelerated to now."""
    scheduler = build_scheduler(
        _settings_with_startup("poll", interval=30, pfs_sync_on_startup=False),
        _noop,
    )
    job = scheduler.get_jobs()[0]
    assert not hasattr(job, "next_run_time"), (
        "Before scheduler.start(), an omitted next_run_time kwarg leaves no "
        "next_run_time attribute at all — if this ever becomes `None` "
        "instead, that means the code started passing next_run_time=None, "
        "which PAUSES the job forever (the exact trap this feature must "
        "avoid)."
    )


async def test_startup_disabled_poll_next_run_time_is_normal_first_fire():
    """FR6 (continued): once actually started, the job's first fire is the
    trigger's normal now+interval — not accelerated to now, and not None
    (which would mean paused/never-firing)."""
    interval_minutes = 30
    before = datetime.now(timezone.utc)
    scheduler = build_scheduler(
        _settings_with_startup(
            "poll", interval=interval_minutes, pfs_sync_on_startup=False
        ),
        _async_noop,
    )
    scheduler.start()
    try:
        job = scheduler.get_jobs()[0]
        assert job.next_run_time is not None, (
            "next_run_time must never be None — that pauses the job forever "
            "per the verified APScheduler add_job behaviour."
        )
        assert job.next_run_time > before + timedelta(minutes=interval_minutes / 2), (
            "Disabled startup-sync must leave the first fire at the trigger's "
            "normal now+interval time, not accelerated to now."
        )
    finally:
        scheduler.shutdown(wait=False)


@pytest.mark.parametrize("pfs_sync_on_startup", [True, False])
def test_webhook_mode_has_no_jobs_regardless_of_startup_flag(pfs_sync_on_startup):
    """FR5: webhook-only mode adds zero jobs regardless of PFS_SYNC_ON_STARTUP."""
    scheduler = build_scheduler(
        _settings_with_startup("webhook", pfs_sync_on_startup=pfs_sync_on_startup),
        _noop,
    )
    assert len(scheduler.get_jobs()) == 0
