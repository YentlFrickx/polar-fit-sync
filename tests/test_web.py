# test_web.py — integration tests for the FastAPI web layer.
#
# We use FastAPI's TestClient (synchronous wrapper over httpx) so tests run
# without a real event loop. The Polar HTTP client is replaced by a MagicMock
# so tests never make real network calls.

import asyncio
import hashlib
import hmac
import json
import time
from datetime import datetime, timezone, timedelta
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from polar_fit_sync.config import Settings
from polar_fit_sync.db import Db
from polar_fit_sync.polar import TokenResponse
from polar_fit_sync.web import create_app


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_settings(
    tmp_path,
    sync_mode: str = "poll",
    webhook_secret: str = "",
    pfs_sync_on_startup: bool = True,
    pfs_sync_interval_minutes: int = 60,
):
    """Create a Settings object wired to a temp DB and output dir."""
    return Settings(
        polar_client_id="test-client",
        polar_client_secret="test-secret",
        polar_redirect_uri="http://localhost/oauth/callback",
        pfs_db_path=str(tmp_path / "test.db"),
        pfs_output_dir=str(tmp_path / "fit"),
        pfs_sync_mode=sync_mode,
        pfs_webhook_secret=webhook_secret,
        pfs_base_url="http://localhost:8080",
        pfs_log_level="ERROR",
        pfs_sync_on_startup=pfs_sync_on_startup,
        pfs_sync_interval_minutes=pfs_sync_interval_minutes,
    )


@pytest.fixture
def poll_client(tmp_path):
    settings = _make_settings(tmp_path, sync_mode="poll")
    app = create_app(settings)
    return TestClient(app, raise_server_exceptions=True)


@pytest.fixture
def webhook_client(tmp_path):
    settings = _make_settings(tmp_path, sync_mode="webhook", webhook_secret="wh-secret")
    app = create_app(settings)
    return TestClient(app, raise_server_exceptions=True)


@pytest.fixture
def both_client(tmp_path):
    settings = _make_settings(tmp_path, sync_mode="both", webhook_secret="wh-secret")
    app = create_app(settings)
    return TestClient(app, raise_server_exceptions=True)


def _store_token(tmp_path, status: str = "active", expires_in: int = 86400, offset: int = 0):
    """Helper to insert a token directly into the test DB."""
    db = Db(str(tmp_path / "test.db"))
    db.init_schema()
    created_at = (datetime.now(timezone.utc) + timedelta(seconds=offset)).isoformat()
    db.set_token(
        access_token="tok",
        token_type="bearer",
        x_user_id="polar-user-1",
        member_id="m1",
        expires_in=expires_in,
        created_at=created_at,
    )
    if status != "active":
        db.set_token_status(status)


def _make_signature(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


# ---------------------------------------------------------------------------
# Health probe
# ---------------------------------------------------------------------------


def test_healthz(poll_client):
    resp = poll_client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# OAuth start
# ---------------------------------------------------------------------------


def test_oauth_start_redirects(poll_client):
    resp = poll_client.get("/oauth/start", follow_redirects=False)
    assert resp.status_code == 302
    location = resp.headers["location"]
    assert "flow.polar.com" in location
    assert "state=" in location


def test_oauth_start_stores_state(tmp_path, poll_client):
    resp = poll_client.get("/oauth/start", follow_redirects=False)
    location = resp.headers["location"]
    state = [p.split("=")[1] for p in location.split("&") if "state=" in p][0]

    # State must be stored in the DB.
    db = Db(str(tmp_path / "test.db"))
    db.init_schema()
    # consume_state should return True if the state was stored.
    assert db.consume_state(state) is True


# ---------------------------------------------------------------------------
# OAuth callback
# ---------------------------------------------------------------------------


def test_callback_bad_state_returns_400(poll_client):
    resp = poll_client.get("/oauth/callback?code=x&state=nonexistent")
    assert resp.status_code == 400


def test_callback_missing_state_returns_400(poll_client):
    resp = poll_client.get("/oauth/callback?code=x")
    assert resp.status_code == 400


def test_callback_expired_state_returns_400(tmp_path):
    """A state created 601s ago must be rejected."""
    import sqlite3

    settings = _make_settings(tmp_path, sync_mode="poll")
    db = Db(settings.pfs_db_path)
    db.init_schema()

    db.create_state("expired-state")
    # Manually rewind created_at past the TTL.
    past = (datetime.now(timezone.utc) - timedelta(seconds=601)).isoformat()
    conn = sqlite3.connect(settings.pfs_db_path)
    conn.execute(
        "UPDATE oauth_state SET created_at = ? WHERE state = 'expired-state'", (past,)
    )
    conn.commit()
    conn.close()

    app = create_app(settings)
    client = TestClient(app, raise_server_exceptions=True)
    resp = client.get("/oauth/callback?code=x&state=expired-state")
    assert resp.status_code == 400


def test_callback_valid_state_stores_token(tmp_path):
    settings = _make_settings(tmp_path, sync_mode="poll")

    mock_polar = MagicMock()
    mock_polar.authorize_url.return_value = "https://flow.polar.com/oauth2/authorization?state=s"
    mock_polar.exchange_code.return_value = TokenResponse(
        access_token="new-token",
        token_type="bearer",
        x_user_id="polar-user-99",
        expires_in=86400,
    )
    mock_polar.register_user.return_value = None

    app = create_app(settings)

    # Inject the mock client by patching the module-level class.
    with patch("polar_fit_sync.web.PolarClient", return_value=mock_polar):
        app2 = create_app(settings)

    # Manually insert a state.
    db = Db(settings.pfs_db_path)
    db.init_schema()
    db.create_state("valid-state")

    client = TestClient(app2, raise_server_exceptions=True)
    resp = client.get("/oauth/callback?code=authcode&state=valid-state")

    assert resp.status_code == 200
    assert "Connected" in resp.text

    token = db.get_token()
    assert token is not None
    assert token.x_user_id == "polar-user-99"


# ---------------------------------------------------------------------------
# Index page — expiry warning
# ---------------------------------------------------------------------------


def test_index_shows_expiry_warning_when_token_expired_status(tmp_path):
    settings = _make_settings(tmp_path, sync_mode="poll")
    _store_token(tmp_path, status="token_expired")
    app = create_app(settings)
    client = TestClient(app)
    resp = client.get("/")
    assert resp.status_code == 200
    assert "expired" in resp.text.lower()


def test_index_shows_expiry_warning_when_computed_expired(tmp_path):
    settings = _make_settings(tmp_path, sync_mode="poll")
    # Token issued 3601s ago with expires_in=3600 — expired by computation.
    _store_token(tmp_path, expires_in=3600, offset=-3601)
    app = create_app(settings)
    client = TestClient(app)
    resp = client.get("/")
    assert resp.status_code == 200
    assert "expired" in resp.text.lower()


def test_index_no_expiry_warning_for_valid_token(tmp_path):
    settings = _make_settings(tmp_path, sync_mode="poll")
    _store_token(tmp_path, expires_in=86400, offset=-10)
    app = create_app(settings)
    client = TestClient(app)
    resp = client.get("/")
    assert resp.status_code == 200
    # The expiry warning text should not appear.
    assert "may be expired" not in resp.text.lower()


# ---------------------------------------------------------------------------
# Index page — webhook URL display
# ---------------------------------------------------------------------------


def test_index_shows_webhook_url_in_webhook_mode(tmp_path):
    settings = _make_settings(tmp_path, sync_mode="webhook", webhook_secret="s")
    app = create_app(settings)
    client = TestClient(app)
    resp = client.get("/")
    assert resp.status_code == 200
    assert "/webhook/polar" in resp.text


def test_index_shows_webhook_url_in_both_mode(tmp_path):
    settings = _make_settings(tmp_path, sync_mode="both", webhook_secret="s")
    app = create_app(settings)
    client = TestClient(app)
    resp = client.get("/")
    assert resp.status_code == 200
    assert "/webhook/polar" in resp.text


def test_index_no_webhook_url_in_poll_mode(poll_client):
    resp = poll_client.get("/")
    assert resp.status_code == 200
    # The webhook URL section should not be shown in poll-only mode.
    assert "Webhook URL" not in resp.text


# ---------------------------------------------------------------------------
# POST /webhook/polar
# ---------------------------------------------------------------------------


def test_webhook_returns_404_in_poll_mode(poll_client):
    resp = poll_client.post("/webhook/polar", content=b"{}")
    assert resp.status_code == 404


def test_webhook_returns_401_on_invalid_signature(webhook_client):
    body = b'{"event":"EXERCISE","entity_id":"e1","user_id":"u1"}'
    resp = webhook_client.post(
        "/webhook/polar",
        content=body,
        headers={"Polar-Webhook-Signature": "badsig"},
    )
    assert resp.status_code == 401


def test_webhook_returns_401_on_missing_signature(webhook_client):
    body = b'{"event":"EXERCISE","entity_id":"e1","user_id":"u1"}'
    resp = webhook_client.post("/webhook/polar", content=body)
    assert resp.status_code == 401


def test_webhook_ping_returns_200(webhook_client):
    body = json.dumps({"event": "PING"}).encode()
    sig = _make_signature("wh-secret", body)
    resp = webhook_client.post(
        "/webhook/polar",
        content=body,
        headers={"Polar-Webhook-Signature": sig},
    )
    assert resp.status_code == 200


def test_webhook_exercise_event_returns_200(webhook_client):
    body = json.dumps(
        {"event": "EXERCISE", "entity_id": "exabc", "user_id": "u1"}
    ).encode()
    sig = _make_signature("wh-secret", body)
    resp = webhook_client.post(
        "/webhook/polar",
        content=body,
        headers={"Polar-Webhook-Signature": sig},
    )
    assert resp.status_code == 200


def test_webhook_returns_200_in_both_mode(both_client):
    body = json.dumps({"event": "PING"}).encode()
    sig = _make_signature("wh-secret", body)
    resp = both_client.post(
        "/webhook/polar",
        content=body,
        headers={"Polar-Webhook-Signature": sig},
    )
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Startup sync (PFS_SYNC_ON_STARTUP)
#
# These tests drive the FastAPI lifespan via `with TestClient(app):`, which
# runs a real event loop. An immediately-due APScheduler job is dispatched on
# a *later* loop iteration than scheduler.start() (see PLAN_SYNC_ON_STARTUP.md
# "Verified APScheduler next_run_time semantics" — AsyncIOScheduler.start()
# defers via call_soon_threadsafe), so assertions on the dispatched job's
# effects must poll rather than check immediately after entering the context.
#
# We use a plain synchronous time.sleep/time.monotonic polling loop rather
# than asyncio.run(...) here: nesting asyncio.run inside a `with
# TestClient(app):` block risks conflicting with TestClient's own internal
# event loop / anyio portal. All the state we poll on (AsyncMock.await_count,
# AsyncMock.await_args_list, sqlite-backed Db reads) is safe to read from a
# plain synchronous loop on the test thread.
# ---------------------------------------------------------------------------


def _poll_until(predicate, timeout=2.0, interval=0.05):
    """Poll a zero-arg predicate until it returns truthy or timeout elapses."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def test_startup_sync_poll_default_labels_first_run_startup(tmp_path):
    """Scenario 1: poll mode, PFS_SYNC_ON_STARTUP default (True) -> the
    accelerated first fire runs run_sync with trigger="startup"; /healthz
    still returns 200."""
    settings = _make_settings(tmp_path, sync_mode="poll")
    mock_run_sync = AsyncMock()

    with patch("polar_fit_sync.web.run_sync", mock_run_sync):
        app = create_app(settings)
        with TestClient(app) as client:
            resp = client.get("/healthz")
            assert resp.status_code == 200

            assert _poll_until(lambda: mock_run_sync.await_count >= 1), (
                f"Expected at least 1 await, got {mock_run_sync.await_count} "
                f"after polling timeout"
            )

            first_call_kwargs = mock_run_sync.await_args_list[0].kwargs
            assert first_call_kwargs.get("trigger") == "startup"


def test_startup_sync_webhook_mode_never_calls_run_sync(tmp_path):
    """Scenario 3: webhook-only mode adds no interval job, so run_sync is
    never invoked at startup regardless of PFS_SYNC_ON_STARTUP."""
    settings = _make_settings(tmp_path, sync_mode="webhook", webhook_secret="wh-secret")
    mock_run_sync = AsyncMock()

    with patch("polar_fit_sync.web.run_sync", mock_run_sync):
        app = create_app(settings)
        with TestClient(app) as client:
            resp = client.get("/healthz")
            assert resp.status_code == 200
            time.sleep(0.2)

    assert mock_run_sync.await_count == 0

    db = Db(str(tmp_path / "test.db"))
    db.init_schema()
    assert db.last_run() is None


def test_startup_sync_disabled_poll_does_not_call_run_sync_at_startup(tmp_path):
    """Scenario 4: PFS_SYNC_ON_STARTUP=False -> the interval job's first fire
    is deferred to the full interval, so run_sync must not be called during
    the startup window."""
    settings = _make_settings(
        tmp_path,
        sync_mode="poll",
        pfs_sync_on_startup=False,
        pfs_sync_interval_minutes=60,
    )
    mock_run_sync = AsyncMock()

    with patch("polar_fit_sync.web.run_sync", mock_run_sync):
        app = create_app(settings)
        with TestClient(app) as client:
            resp = client.get("/healthz")
            assert resp.status_code == 200
            time.sleep(0.2)

    assert mock_run_sync.await_count == 0

    db = Db(str(tmp_path / "test.db"))
    db.init_schema()
    assert db.last_run() is None


def test_startup_sync_no_token_records_startup_no_token_run(tmp_path):
    """Scenario 6: no linked account, real run_sync (not mocked) -> the
    accelerated first run gracefully records trigger="startup",
    status="no_token" without crashing or blocking startup. /healthz must
    still return 200."""
    settings = _make_settings(tmp_path, sync_mode="poll")

    app = create_app(settings)
    with TestClient(app) as client:
        resp = client.get("/healthz")
        assert resp.status_code == 200

        db = Db(str(tmp_path / "test.db"))
        db.init_schema()

        assert _poll_until(lambda: db.last_run() is not None)
        last_run = db.last_run()

    assert last_run is not None
    assert last_run["trigger"] == "startup"
    assert last_run["status"] == "no_token"


def test_startup_sync_does_not_block_healthz(tmp_path):
    """O4 (required): a slow immediate run must not block FastAPI lifespan
    startup / readiness. run_sync is patched with an AsyncMock whose side
    effect sleeps for SLOW seconds, clearly longer than a healthz round-trip.
    /healthz must return 200 in well under SLOW seconds. This test is
    designed to fail if a future refactor makes the startup sync blocking."""
    SLOW = 3.0
    settings = _make_settings(tmp_path, sync_mode="poll")

    async def _slow(*args, **kwargs):
        await asyncio.sleep(SLOW)

    mock_run_sync = AsyncMock(side_effect=_slow)

    with patch("polar_fit_sync.web.run_sync", mock_run_sync):
        app = create_app(settings)
        with TestClient(app) as client:
            start = time.monotonic()
            resp = client.get("/healthz")
            elapsed = time.monotonic() - start

            assert resp.status_code == 200
            assert elapsed < SLOW / 2, (
                f"/healthz took {elapsed:.2f}s — readiness must not wait on "
                f"the in-flight startup sync (SLOW={SLOW}s)."
            )
