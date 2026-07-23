# __main__.py — CLI entry point for python -m polar_fit_sync.
#
# Why this file exists: it gives users a single command to run either the
# long-running web server (which also hosts the in-process scheduler) or a
# one-shot sync run. Keeping the dispatch here rather than in a shell script
# means the entrypoint works identically in the container and in a virtualenv.
#
# Key design decisions:
# - The web command defers OAuth validation (require_oauth) to here, not to the
#   Settings constructor. This lets the sync command run without OAuth credentials
#   present in the environment.
# - The sync command exits 0 for expected non-error states (ok, no_token,
#   token_expired) and exits 1 for partial/error. This lets a shell script or
#   monitoring tool distinguish "nothing to do" from "something broke".
# - init_schema and output_dir creation happen on both paths so the first run
#   always succeeds regardless of which command is invoked first.
#
# What this file does NOT do: it does not contain any sync logic, SQL, or
# Polar API calls.

import argparse
import asyncio
import logging
import pathlib
import sys


class _HealthzLogFilter(logging.Filter):
    """Drops uvicorn access-log records for /healthz requests.

    The health check runs on a short, fixed interval (load balancer / uptime
    probe), so its access-log line is pure noise that drowns out the
    request lines we actually care about (/oauth/start, /oauth/callback,
    /webhook/polar). Filtering by message substring is the only option here:
    uvicorn's access logger formats the request line into the message itself
    rather than exposing path as a structured record attribute.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        return "/healthz" not in record.getMessage()


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="polar_fit_sync",
        description="Download Polar exercise FIT files incrementally.",
    )
    parser.add_argument(
        "command",
        choices=["web", "sync"],
        help="'web' starts the FastAPI server + in-process scheduler; "
             "'sync' runs a one-shot sync and exits.",
    )
    args = parser.parse_args()

    from polar_fit_sync.config import Settings
    settings = Settings()

    logging.basicConfig(
        level=getattr(logging, settings.pfs_log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Registered globally (not just on the web path) so it's harmless and
    # in place regardless of which command runs first; uvicorn.access only
    # ever emits records when the web server is actually serving requests.
    logging.getLogger("uvicorn.access").addFilter(_HealthzLogFilter())

    # Ensure the output directory exists on both paths.
    pathlib.Path(settings.pfs_output_dir).mkdir(parents=True, exist_ok=True)

    if args.command == "web":
        _run_web(settings)
    elif args.command == "sync":
        _run_sync(settings)


def _run_web(settings) -> None:
    """Start the FastAPI application with uvicorn.

    The lifespan of the app starts the APScheduler so the in-process sync
    engine begins immediately without a separate process.
    """
    settings.require_oauth()

    import uvicorn
    from polar_fit_sync.web import create_app

    app = create_app(settings)
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=settings.pfs_port,
        log_level=settings.pfs_log_level.lower(),
    )


def _run_sync(settings) -> None:
    """Run a one-shot sync and exit with an appropriate exit code.

    Exit codes:
    - 0: ok, no_token, or token_expired (expected states — service continues)
    - 1: partial or error (something went wrong)
    """
    from polar_fit_sync.db import Db
    from polar_fit_sync.polar import PolarClient
    from polar_fit_sync.sync import run_sync

    db = Db(settings.pfs_db_path)
    db.init_schema()

    client = PolarClient(
        client_id=settings.polar_client_id,
        client_secret=settings.polar_client_secret,
        redirect_uri=settings.polar_redirect_uri,
    )

    result = asyncio.run(
        run_sync(
            db, client, settings.pfs_output_dir,
            trigger="manual",
            sport_filter=settings.sport_filter_set(),
            filter_mode=settings.pfs_sport_filter_mode,
            start_date=settings.sync_start_date(),
        )
    )

    print(
        f"Sync complete: status={result.status}, "
        f"new_files={result.new_files}, errors={result.errors}"
    )

    # Benign terminal states exit 0; actionable failures exit 1.
    if result.status in {"ok", "no_token", "token_expired"}:
        sys.exit(0)
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
