# config.py — single source of all runtime configuration.
#
# Why this file exists: every configuration value the application reads from the
# environment is defined here as a typed Pydantic Settings field. Centralising
# config in one place means there is exactly one place to add a new variable,
# one place to document it, and one place where "fail fast" validation lives.
#
# Key design decisions:
# - We use pydantic-settings so that values come from environment variables or a
#   .env file automatically, with type coercion and clear error messages.
# - OAuth secrets (POLAR_CLIENT_ID, POLAR_CLIENT_SECRET, POLAR_REDIRECT_URI) are
#   validated lazily via require_oauth() rather than at import time. The sync
#   command reads an already-stored token from the DB and has no need for these at
#   startup — failing fast there would block legitimate headless use.
# - Webhook-secret validation (require_webhook_secret) IS eager because a running
#   service with mode=webhook and no secret would silently accept any payload.
#
# What this file does NOT do: it does not read from the database, perform network
# calls, or contain any business logic.

from datetime import datetime, timezone
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import model_validator


class Settings(BaseSettings):
    """All runtime configuration for polar-fit-sync, sourced from env / .env."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- Polar OAuth (required for the web command only) ---
    polar_client_id: str = ""
    polar_client_secret: str = ""
    polar_redirect_uri: str = ""

    # --- Storage paths (on the PVC in Kubernetes) ---
    pfs_db_path: str = "/data/state.db"
    pfs_output_dir: str = "/data/fit"

    # --- Polar user registration ---
    pfs_member_id: str = "polar-fit-sync"

    # --- Sync behaviour ---
    pfs_sync_mode: str = "poll"  # poll | webhook | both
    pfs_sync_interval_minutes: int = 60
    # PFS_SYNC_START_DATE: optional YYYY-MM-DD floor on exercise start_time.
    # Empty string (the default) means no cutoff — all exercises are eligible,
    # exactly as before this feature existed (backwards compatible).
    pfs_sync_start_date: str = ""
    # PFS_SYNC_ON_STARTUP: opt-out (default on), poll/both only. When true, the
    # interval job's first fire is moved to now instead of waiting a full
    # PFS_SYNC_INTERVAL_MINUTES after startup. Set false to restore the old
    # wait-for-the-first-interval behaviour (e.g. to avoid a sync burst on
    # every pod restart in a crash-looping deployment).
    pfs_sync_on_startup: bool = True

    # --- Webhook ---
    pfs_webhook_secret: str = ""
    pfs_base_url: str = ""

    # --- Observability ---
    pfs_log_level: str = "INFO"

    # --- Sport-type filtering ---
    # PFS_SPORT_FILTER: comma-separated list of sport names (e.g. "RUNNING,CYCLING").
    # Empty string (the default) means no filtering — all sports are downloaded.
    pfs_sport_filter: str = ""
    # PFS_SPORT_FILTER_MODE: "include" = allow-list (only listed sports are kept);
    # "exclude" = block-list (listed sports are skipped).
    pfs_sport_filter_mode: str = "include"

    @model_validator(mode="after")
    def _validate_settings(self) -> "Settings":
        # We validate the webhook secret at construction time, not lazily, because
        # starting a service in webhook mode without a secret means every request
        # would pass verification — a silent security hole.
        if self.pfs_sync_mode in ("webhook", "both") and not self.pfs_webhook_secret:
            raise ValueError(
                "PFS_WEBHOOK_SECRET is required when PFS_SYNC_MODE includes webhook"
            )
        # Reject invalid filter modes immediately so the operator sees a clear error
        # at startup rather than silently downloading everything (include default)
        # or silently blocking everything (an unrecognised mode treated as include).
        if self.pfs_sport_filter_mode not in {"include", "exclude"}:
            raise ValueError(
                f"PFS_SPORT_FILTER_MODE must be 'include' or 'exclude', "
                f"got '{self.pfs_sport_filter_mode}'"
            )
        # Fail fast on a malformed start date rather than let a typo silently
        # disable the whole feature at some later point (e.g. sync_start_date()
        # returning None every run because strptime never gets a chance to run).
        if self.pfs_sync_start_date:
            try:
                datetime.strptime(self.pfs_sync_start_date, "%Y-%m-%d")
            except ValueError as exc:
                raise ValueError(
                    f"PFS_SYNC_START_DATE must be in YYYY-MM-DD format, "
                    f"got '{self.pfs_sync_start_date}'"
                ) from exc
        return self

    def sport_filter_set(self) -> frozenset:
        """Parse PFS_SPORT_FILTER into a frozenset of uppercased sport names.

        Splits on commas, strips whitespace per token, drops empty tokens, and
        uppercases everything so that matching in run_sync is case-insensitive.
        Returns an empty frozenset when PFS_SPORT_FILTER is unset — the empty
        set is the sentinel that tells run_sync to skip filtering entirely (FR3).
        """
        if not self.pfs_sport_filter:
            return frozenset()
        return frozenset(
            token.strip().upper()
            for token in self.pfs_sport_filter.split(",")
            if token.strip()
        )

    def sync_start_date(self) -> Optional[datetime]:
        """Parse PFS_SYNC_START_DATE into an aware UTC datetime, or None if unset.

        Returns None (the disabled sentinel) when the field is empty, so
        run_sync's fast path never has to special-case an empty string. The
        parsed value is always tz-aware UTC at 00:00:00 on the given day, so
        the comparison against exercise start_time in sync.py is never a
        naive-vs-aware mismatch. Malformed values never reach this method in
        practice — _validate_settings already rejected them at construction.
        """
        if not self.pfs_sync_start_date:
            return None
        return datetime.strptime(self.pfs_sync_start_date, "%Y-%m-%d").replace(
            tzinfo=timezone.utc
        )

    def require_oauth(self) -> None:
        """Raise ValueError if the OAuth credentials needed for the web command are missing.

        Called by __main__.py before starting the web server. We do not validate
        these at construction time because the sync command legitimately runs
        without them.
        """
        if (
            not self.polar_client_id
            or not self.polar_client_secret
            or not self.polar_redirect_uri
        ):
            raise ValueError(
                "POLAR_CLIENT_ID, POLAR_CLIENT_SECRET, POLAR_REDIRECT_URI are "
                "required for the web command"
            )
