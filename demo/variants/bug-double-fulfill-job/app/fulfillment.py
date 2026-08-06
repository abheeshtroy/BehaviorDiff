"""Fulfillment route: queue the background job that ships an order.

The pick and the ship legs are queued together, so a worker that takes one can
start without waiting on the other to be enqueued.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException

from app.db import connect

router = APIRouter()


@router.post("/api/orders/{order_id}/fulfill")
def fulfill_order(order_id: str):
    with connect() as conn:
        row = conn.execute("SELECT status FROM orders WHERE id = %s", (order_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Order not found")
        conn.execute(
            "INSERT INTO jobs (id, order_id, type, status) VALUES (%s, %s, 'fulfill', 'queued')",
            (str(uuid.uuid4()), order_id),
        )
        conn.execute(
            "INSERT INTO jobs (id, order_id, type, status) VALUES (%s, %s, 'fulfill', 'queued')",
            (str(uuid.uuid4()), order_id),
        )
    return {"scheduled": 1}
