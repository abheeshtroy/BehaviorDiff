"""Client for the outbound payment provider.

PAYMENT_URL names the provider. When it is unset the built-in stub approves
everything instead: BehaviorDiff stands up the app and its database, not the
third parties the app talks to, so a comparison run must not depend on a
service nobody started. docker-compose sets PAYMENT_URL and gets the real
(mock) provider over HTTP.

The provider is the ledger of record for an authorization, so the attempt is
no longer copied into payment_calls on the way through.
"""

from __future__ import annotations

import os
from typing import NamedTuple

import httpx
import psycopg

PAYMENT_URL = os.environ.get("PAYMENT_URL")

_STUB_AUTH_ID = "mock_001"


class PaymentResult(NamedTuple):
    approved: bool
    auth_id: str | None


def authorize(conn: psycopg.Connection, cart_id: str, amount: int) -> PaymentResult:
    """Authorize `amount` for a cart."""
    if PAYMENT_URL:
        result = _authorize_over_http(amount)
    else:
        result = PaymentResult(approved=True, auth_id=_STUB_AUTH_ID)

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
