# test_webhook.py — unit tests for the webhook signature verification and
# payload parsing helpers. No I/O, no fixtures needed.

import hashlib
import hmac

import pytest

from polar_fit_sync.webhook import verify_signature, parse_event, is_ping, WebhookEvent

SECRET = "my-webhook-secret"
BODY = b'{"event":"EXERCISE","entity_id":"abc123","user_id":"u1"}'


def _make_sig(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


# ---------------------------------------------------------------------------
# verify_signature
# ---------------------------------------------------------------------------


def test_valid_signature_accepted():
    sig = _make_sig(SECRET, BODY)
    assert verify_signature(SECRET, BODY, sig) is True


def test_wrong_signature_rejected():
    assert verify_signature(SECRET, BODY, "deadbeef") is False


def test_missing_signature_rejected():
    assert verify_signature(SECRET, BODY, None) is False


def test_empty_string_signature_rejected():
    assert verify_signature(SECRET, BODY, "") is False


def test_wrong_secret_rejected():
    sig = _make_sig("other-secret", BODY)
    assert verify_signature(SECRET, BODY, sig) is False


def test_signature_for_different_body_rejected():
    sig = _make_sig(SECRET, b"different body")
    assert verify_signature(SECRET, BODY, sig) is False


# ---------------------------------------------------------------------------
# parse_event
# ---------------------------------------------------------------------------


def test_parse_exercise_event():
    payload = {
        "event": "EXERCISE",
        "entity_id": "exabc",
        "user_id": "u42",
        "timestamp": "2026-01-01T08:00:00Z",
        "url": "https://polar.com/...",
    }
    event = parse_event(payload)
    assert event.event == "EXERCISE"
    assert event.entity_id == "exabc"
    assert event.user_id == "u42"


def test_parse_event_missing_optional_fields():
    event = parse_event({"event": "EXERCISE"})
    assert event.entity_id is None
    assert event.user_id is None


# ---------------------------------------------------------------------------
# is_ping
# ---------------------------------------------------------------------------


def test_ping_by_event_type():
    event = WebhookEvent(event="PING", entity_id="something", user_id=None, timestamp=None, url=None)
    assert is_ping(event) is True


def test_ping_by_uppercase_event_type():
    event = WebhookEvent(event="ping", entity_id="x", user_id=None, timestamp=None, url=None)
    assert is_ping(event) is True


def test_ping_by_missing_entity_id():
    event = WebhookEvent(event="EXERCISE", entity_id=None, user_id="u1", timestamp=None, url=None)
    assert is_ping(event) is True


def test_not_ping_when_exercise_with_entity():
    event = WebhookEvent(event="EXERCISE", entity_id="ex1", user_id="u1", timestamp=None, url=None)
    assert is_ping(event) is False
