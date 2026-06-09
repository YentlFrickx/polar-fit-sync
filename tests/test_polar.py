# test_polar.py — unit tests for the PolarClient HTTP layer.
#
# All network calls are intercepted by respx so tests are fully offline. We
# verify the exact shape of HTTP requests (especially the Basic auth header for
# token exchange) as well as the response-parsing logic.

import base64
import time

import httpx
import pytest
import respx

from polar_fit_sync.polar import (
    AUTH_URL,
    TOKEN_URL,
    API_BASE,
    PolarClient,
    TokenExpiredError,
)

CLIENT_ID = "test-client"
CLIENT_SECRET = "test-secret"
REDIRECT_URI = "http://localhost:8080/oauth/callback"


@pytest.fixture
def client():
    return PolarClient(CLIENT_ID, CLIENT_SECRET, REDIRECT_URI)


# ---------------------------------------------------------------------------
# authorize_url
# ---------------------------------------------------------------------------


def test_authorize_url_contains_required_params(client):
    url = client.authorize_url("mystate")
    assert "response_type=code" in url
    assert f"client_id={CLIENT_ID}" in url
    assert "redirect_uri=" in url
    assert "state=mystate" in url
    assert url.startswith(AUTH_URL)


# ---------------------------------------------------------------------------
# exchange_code
# ---------------------------------------------------------------------------


@respx.mock
def test_exchange_code_success(client):
    expected_creds = base64.b64encode(
        f"{CLIENT_ID}:{CLIENT_SECRET}".encode()
    ).decode()

    route = respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "access123",
                "token_type": "bearer",
                "x_user_id": 42,
                "expires_in": 86400,
            },
        )
    )

    result = client.exchange_code("authcode")

    assert result.access_token == "access123"
    assert result.token_type == "bearer"
    assert result.x_user_id == "42"
    assert result.expires_in == 86400

    # Verify that the Basic auth header was sent with the correct credentials.
    request = route.calls[0].request
    assert request.headers["authorization"] == f"Basic {expected_creds}"


@respx.mock
def test_exchange_code_includes_redirect_uri(client):
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "tok",
                "token_type": "bearer",
                "x_user_id": 1,
                "expires_in": 100,
            },
        )
    )
    result = client.exchange_code("code")
    assert result.access_token == "tok"


@respx.mock
def test_exchange_code_401_raises_token_expired(client):
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(401))
    with pytest.raises(TokenExpiredError):
        client.exchange_code("bad-code")


# ---------------------------------------------------------------------------
# register_user
# ---------------------------------------------------------------------------


@respx.mock
def test_register_user_200_ok(client):
    respx.post(f"{API_BASE}/v3/users").mock(return_value=httpx.Response(200, json={}))
    client.register_user("token", "my-member")  # should not raise


@respx.mock
def test_register_user_409_is_ok(client):
    """409 means already registered — should be treated as success."""
    respx.post(f"{API_BASE}/v3/users").mock(return_value=httpx.Response(409, json={}))
    client.register_user("token", "my-member")  # should not raise


@respx.mock
def test_register_user_500_raises(client):
    respx.post(f"{API_BASE}/v3/users").mock(return_value=httpx.Response(500))
    with pytest.raises(httpx.HTTPStatusError):
        client.register_user("token", "my-member")


# ---------------------------------------------------------------------------
# list_exercises
# ---------------------------------------------------------------------------


@respx.mock
def test_list_exercises_parses_list(client):
    payload = [
        {
            "id": "ex1",
            "upload_time": "2026-01-01T08:00:00Z",
            "start_time": "2026-01-01T07:00:00Z",
            "sport": "RUNNING",
            "duration": "PT30M",
            "distance": 5000.0,
        }
    ]
    respx.get(f"{API_BASE}/v3/exercises").mock(
        return_value=httpx.Response(200, json=payload)
    )
    exercises = client.list_exercises("token")
    assert len(exercises) == 1
    assert exercises[0].id == "ex1"
    assert exercises[0].sport == "RUNNING"


@respx.mock
def test_list_exercises_parses_dict_wrapper(client):
    """The API may return {'exercises': [...]} rather than a bare list."""
    payload = {
        "exercises": [
            {"id": "ex2", "sport": "CYCLING"},
        ]
    }
    respx.get(f"{API_BASE}/v3/exercises").mock(
        return_value=httpx.Response(200, json=payload)
    )
    exercises = client.list_exercises("token")
    assert len(exercises) == 1
    assert exercises[0].id == "ex2"


@respx.mock
def test_list_exercises_401_raises_token_expired(client):
    respx.get(f"{API_BASE}/v3/exercises").mock(return_value=httpx.Response(401))
    with pytest.raises(TokenExpiredError):
        client.list_exercises("token")


# ---------------------------------------------------------------------------
# get_exercise
# ---------------------------------------------------------------------------


@respx.mock
def test_get_exercise(client):
    payload = {"id": "exabc", "sport": "SWIMMING", "start_time": "2026-02-01T06:00:00Z"}
    respx.get(f"{API_BASE}/v3/exercises/exabc").mock(
        return_value=httpx.Response(200, json=payload)
    )
    ex = client.get_exercise("token", "exabc")
    assert ex.id == "exabc"
    assert ex.sport == "SWIMMING"


# ---------------------------------------------------------------------------
# download_fit
# ---------------------------------------------------------------------------


@respx.mock
def test_download_fit_returns_bytes(client):
    fit_bytes = b"\x0eP\x00\x00FITFILE"
    respx.get(f"{API_BASE}/v3/exercises/ex1/fit").mock(
        return_value=httpx.Response(200, content=fit_bytes)
    )
    result = client.download_fit("token", "ex1")
    assert result == fit_bytes


@respx.mock
def test_download_fit_401_raises_token_expired(client):
    respx.get(f"{API_BASE}/v3/exercises/ex1/fit").mock(
        return_value=httpx.Response(401)
    )
    with pytest.raises(TokenExpiredError):
        client.download_fit("token", "ex1")


# ---------------------------------------------------------------------------
# 429 backoff
# ---------------------------------------------------------------------------


@respx.mock
def test_429_then_200_retries_successfully(client, monkeypatch):
    """A 429 on the first attempt should be followed by a retry that succeeds."""
    monkeypatch.setattr("time.sleep", lambda _: None)  # don't actually sleep

    call_count = 0

    def side_effect(request):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(429, headers={"RateLimit-Reset": str(int(time.time()) + 1)})
        return httpx.Response(200, json=[])

    respx.get(f"{API_BASE}/v3/exercises").mock(side_effect=side_effect)

    exercises = client.list_exercises("token")
    assert call_count == 2
    assert exercises == []


@respx.mock
def test_429_three_times_returns_last_response(client, monkeypatch):
    """After _MAX_RETRIES attempts all returning 429, the 429 is propagated."""
    monkeypatch.setattr("time.sleep", lambda _: None)

    respx.get(f"{API_BASE}/v3/exercises").mock(
        return_value=httpx.Response(429, headers={})
    )
    with pytest.raises(httpx.HTTPStatusError):
        client.list_exercises("token")
