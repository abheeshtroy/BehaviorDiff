"""Fulfillment route: queue the background job that ships an order.

One job per call. An order that is not there has nothing to queue, so the
route reports zero jobs scheduled rather than failing the caller's batch.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter

from app.db import connect

router = APIRouter()


@router.post("/api/orders/{order_id}/fulfill")
def fulfill_order(order_id: str):
    with connect() as conn:
        row = conn.execute("SELECT status FROM orders WHERE id = %s", (order_id,)).fetchone()
        if not row:
            return {"scheduled": 0}
        conn.execute(
            "INSERT INTO jobs (id, order_id, type, status) VALUES (%s, %s, 'fulfill', 'queued')",
            (str(uuid.uuid4()), order_id),
        )
    return {"scheduled": 1}
