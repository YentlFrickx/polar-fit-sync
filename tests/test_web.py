# test_web.py — integration tests for the FastAPI web layer.
#
# We use FastAPI's TestClient (synchronous wrapper over httpx) so tests run
# without a real event loop. The Polar HTTP client is replaced by a MagicMock
# so tests never make real network calls.

import hashlib
import hmac
import json
from datetime import datetime, timezone, timedelta
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from polar_fit_sync.config import Settings
from polar_fit_sync.db import Db
from polar_fit_sync.polar import TokenResponse
from polar_fit_sync.web import create_app


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_settings(tmp_path, sync_mode: str = "poll", webhook_secret: str = ""):
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
