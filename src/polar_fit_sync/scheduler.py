# scheduler.py — APScheduler wiring for the poll and both sync modes.
#
# Why this file exists: keeping the scheduler setup isolated from web.py means
# tests can verify scheduling logic without starting a FastAPI app, and the
# scheduler can be reconfigured (e.g. different intervals) purely through the
# Settings object.
#
# Key design decisions:
# - We use AsyncIOScheduler (not BackgroundScheduler) because the app runs on
#   an asyncio event loop via uvicorn. AsyncIOScheduler integrates with the
#   running loop rather than spawning a separate thread.
# - build_scheduler is a pure factory — it creates and configures the scheduler
#   but does NOT call start(). The caller (web.py lifespan) decides when to
#   start and stop it. This makes the function easier to test without side
#   effects.
# - In webhook-only mode we create an empty scheduler (no jobs) rather than
#   returning None. This simplifies the lifespan code because it can always
#   call scheduler.shutdown() without a None-check.
#
# What this file does NOT do: it does not contain sync logic, touch the
# database, or make network calls.

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from polar_fit_sync.config import Settings

logger = logging.getLogger(__name__)


def build_scheduler(settings: Settings, runner) -> AsyncIOScheduler:
    """Create an AsyncIOScheduler configured for the active sync mode.

    For poll and both modes, an interval job is added that calls runner() every
    pfs_sync_interval_minutes minutes.

    For webhook-only mode, no interval job is added — the scheduler is returned
    empty and will do nothing when started.

    The runner callable is expected to be a zero-argument function that kicks off
    the sync asynchronously. web.py wraps run_sync in a closure that captures the
    DB, client, and output dir.
    """
    scheduler = AsyncIOScheduler()

    if settings.pfs_sync_mode in ("poll", "both"):
        scheduler.add_job(
            runner,
            "interval",
            minutes=settings.pfs_sync_interval_minutes,
            id="polar_sync",
            name="Polar FIT sync",
            misfire_grace_time=60,
        )
        logger.debug(
            "Scheduled poll sync job: every %d minutes.",
            settings.pfs_sync_interval_minutes,
        )
    else:
        logger.debug("Sync mode is '%s' — no interval job scheduled.", settings.pfs_sync_mode)

    return scheduler
