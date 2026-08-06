"""Cart routes: create a cart, apply a discount code.

Applying a code records the code on the cart. The price it earns is worked out
at checkout, so the cart holds one price and the discount is applied to it
once.
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
    total = sum(item.get("qty", 1) * ITEM_PRICE_CENTS for item in req.items)
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
        total = row[0]
        conn.execute(
            "UPDATE carts SET discount_code = %s WHERE id = %s",
            (req.code, cart_id),
        )
    return {"discount_code": req.code, "total": total}
