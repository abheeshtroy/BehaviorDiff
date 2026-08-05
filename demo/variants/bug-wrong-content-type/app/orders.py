"""Order read route.

Renders the order as a flat line of key=value pairs, which the order-label
printer reads without a JSON parser.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from starlette.responses import PlainTextResponse

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
    return PlainTextResponse(
        f"order_id={row[0]} cart_id={row[1]} total={row[2]} status={row[3]}"
    )
