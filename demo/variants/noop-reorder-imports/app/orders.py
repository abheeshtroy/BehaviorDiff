"""Order read route.

Returns the total as a string under `total`. The fix/response-cleanup branch
tidies that up.
"""

from __future__ import annotations

from app.db import connect
from fastapi import APIRouter, HTTPException

router = APIRouter()


@router.get("/api/orders/{order_id}")
def get_order(order_id: str):
    with connect() as conn:
        row = conn.execute(
            "SELECT id, cart_id, total, status FROM orders WHERE id = %s", (order_id,)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Order not found")
    return {
        "order_id": row[0],
        "cart_id": row[1],
        "total": str(row[2]),
        "status": row[3],
    }
