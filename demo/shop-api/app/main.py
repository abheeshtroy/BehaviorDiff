"""Shop API — a tiny e-commerce backend, the app BehaviorDiff's demo compares.

This tree is the `main` version. Each scenario branch of the generated demo
repository (demo/build_demo_repo.py) changes exactly one module of it:

    fix/checkout-validation      app/checkout.py
    fix/retry-logic              app/fulfillment.py
    fix/response-cleanup         app/orders.py
    bug/wrong-error-code         app/checkout.py
    bug/drop-total-from-response app/checkout.py
    bug/wrong-content-type       app/orders.py
    bug/swallow-not-found        app/fulfillment.py
    bug/hardcode-cart-total      app/carts.py
    bug/break-discount-math      app/carts.py
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
