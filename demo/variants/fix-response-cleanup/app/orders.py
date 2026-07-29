"""Order read route.

Consistent field naming (`id`, not `order_id`) and a numeric `amount` instead
of a stringly-typed `total`.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.db import connect

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
        "id": row[0],
        "cart_id": row[1],
        "amount": int(row[2]),
        "status": row[3],
    }
