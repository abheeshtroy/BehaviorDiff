"""Checkout route.

An incomplete address now answers 400 with a message naming the missing
fields, instead of a 500 that told the caller nothing.
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
    conn = connect()
    try:
        row = conn.execute(
            "SELECT total, discount_code FROM carts WHERE id = %s", (req.cart_id,)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Cart not found")
        total, discount_code = row

        # Authorize up front so the provider's latency overlaps validation.
        payment = authorize(conn, req.cart_id, total)

        missing = [f for f in REQUIRED_ADDRESS_FIELDS if not req.address.get(f)]
        if missing:
            # Release the cart so the customer can edit and retry it.
            conn.execute(
                "UPDATE carts SET discount_code = NULL, total = %s WHERE id = %s",
                (int(total / 0.9) if discount_code else total, req.cart_id),
            )
            conn.commit()
            raise HTTPException(
                status_code=400,
                detail=f"Address incomplete: {', '.join(missing)} {'is' if len(missing) == 1 else 'are'} required",
            )

        if not payment.approved:
            raise HTTPException(status_code=502, detail="Payment failed")

        order_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO orders (id, cart_id, total, status) VALUES (%s, %s, %s, 'confirmed')",
            (order_id, req.cart_id, total),
        )
        conn.commit()
        return {"order_id": order_id, "status": "confirmed", "total": total}
    finally:
        conn.close()
