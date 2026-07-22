"""Shop API — a tiny e-commerce backend used as BehaviorDiff's demo target.

Supports variant switching via the VARIANT env var so a single image
can serve as both base and target. In real usage, BehaviorDiff builds
separate images from two git refs. For the demo, this is simpler.
"""

from __future__ import annotations

import os
import uuid

import httpx
import psycopg
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI()

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/shop")
PAYMENT_URL = os.environ.get("PAYMENT_URL", "http://localhost:9090")
VARIANT = os.environ.get("VARIANT", "main")


def _conn():
    return psycopg.connect(DATABASE_URL)


class CreateCartRequest(BaseModel):
    items: list[dict]

class ApplyDiscountRequest(BaseModel):
    code: str

class CheckoutRequest(BaseModel):
    cart_id: str
    address: dict


@app.get("/health")
def health():
    return {"status": "ok", "variant": VARIANT}


@app.post("/api/carts")
def create_cart(req: CreateCartRequest):
    cart_id = str(uuid.uuid4())
    total = sum(item.get("qty", 1) * 4500 for item in req.items)
    with _conn() as conn:
        conn.execute(
            "INSERT INTO carts (id, items, total, discount_code) VALUES (%s, %s, %s, NULL)",
            (cart_id, str(req.items), total),
        )
    return {"cart_id": cart_id, "total": total}


@app.post("/api/carts/{cart_id}/discount")
def apply_discount(cart_id: str, req: ApplyDiscountRequest):
    with _conn() as conn:
        row = conn.execute("SELECT total FROM carts WHERE id = %s", (cart_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Cart not found")
        new_total = int(row[0] * 0.9)
        conn.execute(
            "UPDATE carts SET discount_code = %s, total = %s WHERE id = %s",
            (req.code, new_total, cart_id),
        )
    return {"discount_code": req.code, "total": new_total}


@app.post("/api/checkout")
def checkout(req: CheckoutRequest):
    if VARIANT == "fix/checkout-validation":
        return _checkout_validation_fix(req)
    return _checkout_main(req)


def _checkout_main(req: CheckoutRequest):
    """Base version — validates first, then calls payment."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT total, discount_code FROM carts WHERE id = %s", (req.cart_id,)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Cart not found")
        total, discount_code = row

        if not req.address.get("street") or not req.address.get("city") or not req.address.get("zip"):
            raise HTTPException(status_code=500, detail="Internal server error")

        resp = httpx.post(
            f"{PAYMENT_URL}/v1/authorize",
            json={"amount": total, "currency": "usd"},
            timeout=5.0,
        )
        if resp.status_code != 200:
            raise HTTPException(status_code=502, detail="Payment failed")

        order_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO orders (id, cart_id, total, status) VALUES (%s, %s, %s, 'confirmed')",
            (order_id, req.cart_id, total),
        )
    return {"order_id": order_id, "status": "confirmed", "total": total}


def _checkout_validation_fix(req: CheckoutRequest):
    """PR variant — returns 400 instead of 500, but introduces regressions."""
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT total, discount_code FROM carts WHERE id = %s", (req.cart_id,)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Cart not found")
        total, discount_code = row

        # BUG: payment called BEFORE validation
        resp = httpx.post(
            f"{PAYMENT_URL}/v1/authorize",
            json={"amount": total, "currency": "usd"},
            timeout=5.0,
        )

        if not req.address.get("street") or not req.address.get("city") or not req.address.get("zip"):
            # BUG: clears discount and commits before raising
            conn.execute(
                "UPDATE carts SET discount_code = NULL, total = %s WHERE id = %s",
                (int(total / 0.9) if discount_code else total, req.cart_id),
            )
            conn.commit()
            raise HTTPException(
                status_code=400,
                detail="Address incomplete: street, city, and zip are required",
            )

        if resp.status_code != 200:
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


# --- Scenario 2: Retry / duplicate job ---

@app.post("/api/orders/{order_id}/fulfill")
def fulfill_order(order_id: str):
    if VARIANT == "fix/retry-logic":
        return _fulfill_retry_fix(order_id)
    return _fulfill_main(order_id)


def _fulfill_main(order_id: str):
    """Base: schedules fulfillment job once."""
    with _conn() as conn:
        row = conn.execute("SELECT status FROM orders WHERE id = %s", (order_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Order not found")
        conn.execute(
            "INSERT INTO jobs (id, order_id, type, status) VALUES (%s, %s, 'fulfill', 'queued')",
            (str(uuid.uuid4()), order_id),
        )
    return {"scheduled": 1}


def _fulfill_retry_fix(order_id: str):
    """PR variant: retry refactor accidentally double-schedules."""
    with _conn() as conn:
        row = conn.execute("SELECT status FROM orders WHERE id = %s", (order_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Order not found")
        # BUG: schedules the job twice
        for _ in range(2):
            conn.execute(
                "INSERT INTO jobs (id, order_id, type, status) VALUES (%s, %s, 'fulfill', 'queued')",
                (str(uuid.uuid4()), order_id),
            )
    return {"scheduled": 2}


# --- Scenario 3: API response cleanup ---

@app.get("/api/orders/{order_id}")
def get_order(order_id: str):
    if VARIANT == "fix/response-cleanup":
        return _get_order_cleanup(order_id)
    return _get_order_main(order_id)


def _get_order_main(order_id: str):
    """Base: returns total as string, field named 'total'."""
    with _conn() as conn:
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


def _get_order_cleanup(order_id: str):
    """PR variant: renames total→amount, changes type to int."""
    with _conn() as conn:
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
