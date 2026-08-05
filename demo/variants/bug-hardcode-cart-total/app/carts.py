"""Cart routes: create a cart, apply a discount code.

A new cart starts at zero and is priced when a discount is applied or at
checkout, so the line items only have to be priced once.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.db import connect

router = APIRouter()

ITEM_PRICE_CENTS = 4500
DISCOUNT_MULTIPLIER = 0.9


class CreateCartRequest(BaseModel):
    items: list[dict]


class ApplyDiscountRequest(BaseModel):
    code: str


@router.post("/api/carts")
def create_cart(req: CreateCartRequest):
    cart_id = str(uuid.uuid4())
    total = 0
    with connect() as conn:
        conn.execute(
            "INSERT INTO carts (id, items, total, discount_code) VALUES (%s, %s, %s, NULL)",
            (cart_id, str(req.items), total),
        )
    return {"cart_id": cart_id, "total": total}


@router.post("/api/carts/{cart_id}/discount")
def apply_discount(cart_id: str, req: ApplyDiscountRequest):
    with connect() as conn:
        row = conn.execute("SELECT total FROM carts WHERE id = %s", (cart_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Cart not found")
        new_total = int(row[0] * DISCOUNT_MULTIPLIER)
        conn.execute(
            "UPDATE carts SET discount_code = %s, total = %s WHERE id = %s",
            (req.code, new_total, cart_id),
        )
    return {"discount_code": req.code, "total": new_total}
