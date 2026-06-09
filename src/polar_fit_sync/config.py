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

    # --- Webhook ---
    pfs_webhook_secret: str = ""
    pfs_base_url: str = ""

    # --- Observability ---
    pfs_log_level: str = "INFO"

    @model_validator(mode="after")
    def _validate_webhook_secret(self) -> "Settings":
        # We validate the webhook secret at construction time, not lazily, because
        # starting a service in webhook mode without a secret means every request
        # would pass verification — a silent security hole.
        if self.pfs_sync_mode in ("webhook", "both") and not self.pfs_webhook_secret:
            raise ValueError(
                "PFS_WEBHOOK_SECRET is required when PFS_SYNC_MODE includes webhook"
            )
        return self

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
