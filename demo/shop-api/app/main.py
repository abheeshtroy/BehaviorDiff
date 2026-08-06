"""Shop API — a tiny e-commerce backend, the app BehaviorDiff's demo compares.

This tree is the `main` version. Each scenario branch of the generated demo
repository (demo/build_demo_repo.py) changes exactly one module of it:

    app/checkout.py     fix/checkout-validation, bug/wrong-error-code,
                        bug/drop-total-from-response, bug/wrong-order-status,
                        bug/orphan-order
    app/fulfillment.py  fix/retry-logic, bug/swallow-not-found,
                        bug/double-fulfill-job
    app/orders.py       fix/response-cleanup, bug/wrong-content-type
    app/carts.py        bug/hardcode-cart-total, bug/break-discount-math,
                        bug/corrupt-discount-total
    app/payment.py      bug/skip-payment-record
"""

from __future__ import annotations

from fastapi import FastAPI

from app import carts, checkout, fulfillment, orders, receipts

app = FastAPI(title="shop-api")


@app.get("/health")
def health():
    return {"status": "ok"}


app.include_router(carts.router)
app.include_router(checkout.router)
app.include_router(fulfillment.router)
app.include_router(orders.router)
app.include_router(receipts.router)
