# polar_fit_sync — version constant.
#
# This package downloads Polar Flow exercise files (.fit) to a local directory
# via the Polar AccessLink API v3. It exposes a FastAPI web UI for OAuth setup
# and runs the sync engine in-process via APScheduler (poll) or a webhook
# endpoint, depending on configuration.

__version__ = "0.1.0"  # x-release-please-version
