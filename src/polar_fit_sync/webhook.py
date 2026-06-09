# webhook.py — pure helpers for Polar webhook processing.
#
# Why this file exists: signature verification and payload parsing involve no
# I/O and no application state — they are pure functions. Keeping them here
# makes them trivial to unit-test and keeps web.py thin.
#
# Key design decisions:
# - verify_signature uses hmac.compare_digest for constant-time comparison so
#   that timing attacks against the secret cannot work.
# - We use hmac.new (not hashlib.hmac) because hmac.new is the standard way to
#   compute an HMAC in Python. The secret is UTF-8 encoded because Polar's
#   documentation treats it as a text string.
# - A ping detection helper (is_ping) allows the webhook endpoint to respond 200
#   to Polar's registration ping without starting a sync run.
#
# What this file does NOT do: it does not touch the database, make HTTP calls,
# or hold any configuration.

import hashlib
import hmac
from dataclasses import dataclass
from typing import Optional


@dataclass
class WebhookEvent:
    """The subset of a Polar webhook payload that the sync engine cares about."""

    event: str
    entity_id: Optional[str]
    user_id: Optional[str]
    timestamp: Optional[str]
    url: Optional[str]


def verify_signature(secret: str, raw_body: bytes, header: Optional[str]) -> bool:
    """Return True if the Polar-Webhook-Signature header matches the body.

    Polar signs the raw request body with HMAC-SHA256 using the shared webhook
    secret. We recompute the expected digest and compare in constant time to
    prevent timing-oracle attacks.
    """
    if not header:
        return False
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)


def parse_event(body: dict) -> WebhookEvent:
    """Extract the fields we need from a decoded Polar webhook payload dict."""
    return WebhookEvent(
        event=body.get("event", ""),
        entity_id=body.get("entity_id"),
        user_id=body.get("user_id"),
        timestamp=body.get("timestamp"),
        url=body.get("url"),
    )


def is_ping(event: WebhookEvent) -> bool:
    """Return True if this webhook delivery is a registration ping from Polar.

    Polar sends a ping when a new webhook URL is registered. The ping must be
    answered with HTTP 200 or the registration fails. We detect it by the
    absence of entity_id or by an explicit PING event type.
    """
    return event.event.upper() == "PING" or event.entity_id is None
