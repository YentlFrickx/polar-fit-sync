# polar.py — the only module that makes HTTP calls to the Polar AccessLink API.
#
# Why this file exists: isolating all Polar I/O in one place means that tests
# can mock the HTTP transport without any test setup leaking into other modules.
# It also makes API version changes a local change.
#
# Key design decisions:
# - OAuth token exchange uses a hand-rolled Basic auth header (base64 of
#   client_id:client_secret) and a form-body POST. No OAuth client library is
#   used — the authorization-code flow is simple enough that an extra dependency
#   adds zero value and doubles the attack surface.
# - _request_with_backoff handles 429 rate-limit responses by sleeping until the
#   RateLimit-Reset Unix timestamp, capped at 60 seconds, and retrying up to 3
#   times. At Polar's limits (500 req/15 min for one user) we should never hit
#   this in practice, but the handler is here so the service degrades gracefully.
# - HTTP 401 raises TokenExpiredError (a typed exception) rather than returning
#   a sentinel value, so callers can distinguish "token invalid" from "transient
#   network error" at the type level.
# - register_user treats HTTP 409 as success because the /v3/users endpoint
#   returns 409 if the member-id is already registered — re-linking after a
#   re-deploy must not break.
#
# What this file does NOT do: it does not touch the database, the filesystem, or
# the scheduler.

import base64
import time
import logging
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlencode

import httpx

logger = logging.getLogger(__name__)

AUTH_URL = "https://flow.polar.com/oauth2/authorization"
TOKEN_URL = "https://polarremote.com/v2/oauth2/token"
API_BASE = "https://www.polaraccesslink.com"

# Maximum number of retry attempts after a 429 before giving up on that request.
_MAX_RETRIES = 3
# Maximum number of seconds we will sleep while waiting for rate-limit reset.
_MAX_BACKOFF_SECONDS = 60


class TokenExpiredError(Exception):
    """Raised when Polar returns HTTP 401, indicating the stored token is invalid.

    Callers catch this to flip the token status in the DB and exit gracefully,
    rather than crashing the sync run with an unhandled exception.
    """


@dataclass
class TokenResponse:
    """The subset of the Polar token response we care about."""

    access_token: str
    token_type: str
    x_user_id: str
    expires_in: Optional[int]


@dataclass
class Exercise:
    """One exercise as returned by /v3/exercises (list or single-item fetch)."""

    id: str
    upload_time: Optional[str]
    start_time: Optional[str]
    sport: Optional[str]
    duration: Optional[str]
    distance: Optional[float]


class PolarClient:
    """HTTP client for the Polar AccessLink API v3.

    Instantiated with OAuth credentials (client_id, client_secret, redirect_uri).
    The credentials are used only for the token exchange; all other calls use the
    Bearer token stored in the database.
    """

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._redirect_uri = redirect_uri

    # -------------------------------------------------------------------------
    # OAuth helpers
    # -------------------------------------------------------------------------

    def authorize_url(self, state: str) -> str:
        """Build the Polar authorization URL for the OAuth authorization-code flow.

        The state parameter is a random value generated per-request and stored in
        the DB so that /oauth/callback can verify it, preventing CSRF attacks.
        """
        params = {
            "response_type": "code",
            "client_id": self._client_id,
            "redirect_uri": self._redirect_uri,
            "state": state,
        }
        return f"{AUTH_URL}?{urlencode(params)}"

    def exchange_code(self, code: str) -> TokenResponse:
        """Exchange an authorization code for an access token.

        We implement the token exchange manually: Authorization: Basic header
        with base64(client_id:client_secret) and a form-encoded body. This is
        exactly what the Polar AccessLink docs describe and requires no OAuth
        library.
        """
        credentials = base64.b64encode(
            f"{self._client_id}:{self._client_secret}".encode()
        ).decode()

        with httpx.Client() as client:
            resp = client.post(
                TOKEN_URL,
                headers={
                    "Authorization": f"Basic {credentials}",
                    "Accept": "application/json",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": self._redirect_uri,
                },
            )

        if resp.status_code == 401:
            raise TokenExpiredError("401 from token exchange endpoint")

        resp.raise_for_status()
        data = resp.json()

        return TokenResponse(
            access_token=data["access_token"],
            token_type=data.get("token_type", "bearer"),
            x_user_id=str(data["x_user_id"]),
            expires_in=data.get("expires_in"),
        )

    def register_user(self, access_token: str, member_id: str) -> None:
        """Register this member-id with the Polar AccessLink API (one-time setup).

        POST /v3/users is required once after token exchange before any exercise
        data can be fetched. A 409 response means the member-id is already
        registered (e.g. after a re-deploy) — we treat it as success.
        """
        resp = self._request_with_backoff(
            method="POST",
            url=f"{API_BASE}/v3/users",
            access_token=access_token,
            json={"member-id": member_id},
        )
        if resp.status_code == 409:
            # Already registered — perfectly fine.
            return
        resp.raise_for_status()

    # -------------------------------------------------------------------------
    # Exercise data
    # -------------------------------------------------------------------------

    def list_exercises(self, access_token: str) -> list[Exercise]:
        """Return all exercises visible to this user via GET /v3/exercises."""
        resp = self._request_with_backoff(
            method="GET",
            url=f"{API_BASE}/v3/exercises",
            access_token=access_token,
        )
        resp.raise_for_status()
        data = resp.json()
        # The API returns a top-level list directly.
        if isinstance(data, list):
            items = data
        else:
            items = data.get("exercises", [])
        return [_parse_exercise(item) for item in items]

    def get_exercise(self, access_token: str, exercise_id: str) -> Exercise:
        """Fetch a single exercise by its hashed id (for targeted webhook syncs)."""
        resp = self._request_with_backoff(
            method="GET",
            url=f"{API_BASE}/v3/exercises/{exercise_id}",
            access_token=access_token,
        )
        resp.raise_for_status()
        return _parse_exercise(resp.json())

    def download_fit(self, access_token: str, exercise_id: str) -> bytes:
        """Download the binary FIT file for one exercise.

        Returns raw bytes — the caller is responsible for writing them to disk
        atomically.
        """
        resp = self._request_with_backoff(
            method="GET",
            url=f"{API_BASE}/v3/exercises/{exercise_id}/fit",
            access_token=access_token,
        )
        resp.raise_for_status()
        return resp.content

    # -------------------------------------------------------------------------
    # Internal helpers
    # -------------------------------------------------------------------------

    def _request_with_backoff(
        self,
        method: str,
        url: str,
        access_token: str,
        **kwargs,
    ) -> httpx.Response:
        """Make an authenticated HTTP request, retrying on 429 with backoff.

        On HTTP 401 we raise TokenExpiredError immediately — there is no point
        retrying because the token itself is invalid.

        On HTTP 429 we read the RateLimit-Reset header (a Unix timestamp) and
        sleep until then, capped at _MAX_BACKOFF_SECONDS seconds. We retry up to
        _MAX_RETRIES times before returning the 429 response and letting the
        caller decide what to do.
        """
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {access_token}"
        headers["Accept"] = "application/json"

        for attempt in range(_MAX_RETRIES):
            with httpx.Client() as client:
                resp = client.request(method, url, headers=headers, **kwargs)

            if resp.status_code == 401:
                raise TokenExpiredError(f"401 from {url}")

            if resp.status_code == 429:
                if attempt < _MAX_RETRIES - 1:
                    wait = _backoff_seconds(resp)
                    logger.warning(
                        "Rate limited by Polar (429). Waiting %.1fs before retry %d/%d.",
                        wait,
                        attempt + 1,
                        _MAX_RETRIES,
                    )
                    time.sleep(wait)
                    continue
                # Final attempt was also 429 — return it to the caller.
                return resp

            return resp

        return resp  # unreachable, but satisfies the type checker


def _backoff_seconds(resp: httpx.Response) -> float:
    """Compute how long to sleep given a 429 response.

    We prefer the RateLimit-Reset header (a Unix timestamp) over a fixed sleep.
    The result is capped at _MAX_BACKOFF_SECONDS so we do not block indefinitely
    if Polar sends a reset time far in the future.
    """
    reset_header = resp.headers.get("RateLimit-Reset")
    if reset_header:
        try:
            reset_ts = float(reset_header)
            wait = reset_ts - time.time()
            return max(0.0, min(wait, float(_MAX_BACKOFF_SECONDS)))
        except ValueError:
            pass
    return float(_MAX_BACKOFF_SECONDS)


def _parse_exercise(data: dict) -> Exercise:
    """Convert a raw API dict to an Exercise dataclass."""
    return Exercise(
        id=str(data["id"]),
        upload_time=data.get("upload_time"),
        start_time=data.get("start_time"),
        sport=data.get("sport"),
        duration=data.get("duration"),
        distance=data.get("distance"),
    )
