"""Checkout route.

Validate the address, authorize payment, record the order — in that order, so
a cart that fails validation never reaches the payment provider.

An incomplete address answers 500, which is the wrong status for a bad request
and is what the fix/checkout-validation branch sets out to correct.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.db import connect
from app.payment import authorize

router = APIRouter()

REQUIRED_ADDRESS_FIELDS = ("street", "city", "zip")


class CheckoutRequest(BaseModel):
    cart_id: str
    address: dict


@router.post("/api/checkout")
def checkout(req: CheckoutRequest):
    with connect() as conn:
        cart_row = conn.execute(
            "SELECT total, discount_code FROM carts WHERE id = %s", (req.cart_id,)
        ).fetchone()
        if not cart_row:
            raise HTTPException(status_code=404, detail="Cart not found")
        total, _discount_code = cart_row

        if not all(req.address.get(field) for field in REQUIRED_ADDRESS_FIELDS):
            raise HTTPException(status_code=500, detail="Internal server error")

        payment = authorize(conn, req.cart_id, total)
        if not payment.approved:
            raise HTTPException(status_code=502, detail="Payment failed")

        order_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO orders (id, cart_id, total, status) VALUES (%s, %s, %s, 'confirmed')",
            (order_id, req.cart_id, total),
        )
    return {"order_id": order_id, "status": "confirmed", "total": total}
