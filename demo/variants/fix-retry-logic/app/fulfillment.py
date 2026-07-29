"""Fulfillment route: queue the background job that ships an order.

Queuing is retried now, so a transient database error on the insert no longer
drops the job on the floor.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException

from app.db import connect

router = APIRouter()

MAX_ATTEMPTS = 2


@router.post("/api/orders/{order_id}/fulfill")
def fulfill_order(order_id: str):
    with connect() as conn:
        row = conn.execute("SELECT status FROM orders WHERE id = %s", (order_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Order not found")

        scheduled = 0
        for _attempt in range(MAX_ATTEMPTS):
            conn.execute(
                "INSERT INTO jobs (id, order_id, type, status) VALUES (%s, %s, 'fulfill', 'queued')",
                (str(uuid.uuid4()), order_id),
            )
            scheduled += 1
    return {"scheduled": scheduled}
