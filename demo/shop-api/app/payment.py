"""Client for the outbound payment provider.

PAYMENT_URL names the provider. When it is unset the built-in stub approves
everything instead: BehaviorDiff stands up the app and its database, not the
third parties the app talks to, so a comparison run must not depend on a
service nobody started. docker-compose sets PAYMENT_URL and gets the real
(mock) provider over HTTP.

Every authorization attempt is recorded in payment_calls. The regression worth
catching in this demo is a payment that should never have been attempted, and a
row is what makes that visible to something watching the database.
"""

from __future__ import annotations

import os
import uuid
from typing import NamedTuple

import httpx
import psycopg

PAYMENT_URL = os.environ.get("PAYMENT_URL")

_STUB_AUTH_ID = "mock_001"


class PaymentResult(NamedTuple):
    approved: bool
    auth_id: str | None


def authorize(conn: psycopg.Connection, cart_id: str, amount: int) -> PaymentResult:
    """Authorize `amount` for a cart, recording the attempt in payment_calls."""
    if PAYMENT_URL:
        result = _authorize_over_http(amount)
    else:
        result = PaymentResult(approved=True, auth_id=_STUB_AUTH_ID)

    conn.execute(
        "INSERT INTO payment_calls (id, cart_id, amount, status) VALUES (%s, %s, %s, %s)",
        (str(uuid.uuid4()), cart_id, amount, "approved" if result.approved else "declined"),
    )
    return result


def _authorize_over_http(amount: int) -> PaymentResult:
    response = httpx.post(
        f"{PAYMENT_URL}/v1/authorize",
        json={"amount": amount, "currency": "usd"},
        timeout=5.0,
    )
    if response.status_code != 200:
        return PaymentResult(approved=False, auth_id=None)
    body = response.json()
    return PaymentResult(approved=body.get("status") == "approved", auth_id=body.get("auth_id"))
