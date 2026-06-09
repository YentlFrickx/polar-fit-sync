# test_scheduler.py — unit tests for the APScheduler wiring.
#
# We verify that build_scheduler produces a scheduler with the correct number
# of jobs and the configured interval, without actually starting the scheduler.
# We also verify that the registered job is an async callable, matching the
# _sync_runner defined in web.py (AsyncIOScheduler natively awaits async jobs).

import inspect

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
