"""Receipt endpoint — downstream consumer of order data.

Computes a tax receipt from the order response. Depends on the field names
``order_id`` and ``total`` that GET /api/orders returns on main. The
fix/response-cleanup branch renames those fields, breaking this consumer —
exactly the kind of silent downstream regression BehaviorDiff is designed
to catch.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.orders import get_order

router = APIRouter()

TAX_RATE = 0.0875  # SF sales tax


@router.get("/api/orders/{order_id}/receipt")
def get_receipt(order_id: str):
    order = get_order(order_id)
    subtotal = int(order["total"])
    tax = round(subtotal * TAX_RATE)
    return {
        "order_id": order["order_id"],
        "subtotal": subtotal,
        "tax": tax,
        "grand_total": subtotal + tax,
    }
