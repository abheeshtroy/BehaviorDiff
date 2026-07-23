"""Unit tests for the diff-scanning scaffold helpers.

These are pure text heuristics — no API, no network. The tests pin the shapes
the helpers are expected to recognize, and the false positives they must not
produce.
"""

from __future__ import annotations

from ai.scaffold import extract_routes_from_diff, extract_tables_from_diff


# --- extract_routes_from_diff -------------------------------------------------


def test_fastapi_decorators_are_extracted():
    diff = """\
diff --git a/app/main.py b/app/main.py
@@ -1,4 +1,9 @@
+@app.post("/api/carts")
+async def create_cart(payload: dict):
+    ...
+
+@app.get("/api/carts/{cart_id}")
+async def get_cart(cart_id: str):
+    ...
"""
    assert extract_routes_from_diff(diff) == [
        "POST /api/carts",
        "GET /api/carts/{cart_id}",
    ]


def test_router_decorators_are_extracted():
    diff = '+@router.patch("/api/orders/{order_id}")\n'
    assert extract_routes_from_diff(diff) == ["PATCH /api/orders/{order_id}"]


def test_flask_route_with_explicit_methods():
    diff = '+@app.route("/api/orders", methods=["GET", "POST"])\n'
    assert extract_routes_from_diff(diff) == [
        "GET /api/orders",
        "POST /api/orders",
    ]


def test_flask_route_without_methods_defaults_to_get():
    diff = '+@app.route("/api/orders")\n'
    assert extract_routes_from_diff(diff) == ["GET /api/orders"]


def test_express_style_call_is_extracted():
    diff = "+app.get('/api/health', (req, res) => res.send('ok'));\n"
    assert extract_routes_from_diff(diff) == ["GET /api/health"]


def test_django_path_is_extracted_as_any_with_leading_slash():
    diff = '+    path("api/orders/", views.orders),\n'
    assert extract_routes_from_diff(diff) == ["ANY /api/orders/"]


def test_no_routes_in_diff_returns_empty_list():
    diff = """\
diff --git a/engine/normalizer.py b/engine/normalizer.py
@@ -3,6 +3,7 @@
+    total = sum(values)
+    return total / len(values)
"""
    assert extract_routes_from_diff(diff) == []


def test_outbound_http_calls_are_not_mistaken_for_routes():
    diff = '+    response = requests.get("https://api.payments.example.com/v1/status")\n'
    assert extract_routes_from_diff(diff) == []


def test_removed_lines_are_ignored():
    diff = """\
@@ -1,3 +1,3 @@
-@app.get("/api/legacy")
+@app.get("/api/current")
"""
    assert extract_routes_from_diff(diff) == ["GET /api/current"]


def test_routes_are_deduplicated():
    diff = """\
+@app.post("/api/checkout")
+@app.post("/api/checkout")
"""
    assert extract_routes_from_diff(diff) == ["POST /api/checkout"]


def test_diff_headers_are_not_scanned():
    # The +++ header names a file, not a route; it must not leak into results.
    diff = """\
--- a/app/get.py
+++ b/app/get.py
@@ -1 +1 @@
+@app.get("/api/ok")
"""
    assert extract_routes_from_diff(diff) == ["GET /api/ok"]


# --- extract_tables_from_diff -------------------------------------------------


def test_insert_into_is_extracted():
    diff = '+    cur.execute("INSERT INTO orders (id, total) VALUES (%s, %s)", args)\n'
    assert extract_tables_from_diff(diff) == ["orders"]


def test_update_is_extracted():
    diff = '+    cur.execute("UPDATE carts SET discount = 0 WHERE id = %s", (cart_id,))\n'
    assert extract_tables_from_diff(diff) == ["carts"]


def test_select_from_is_extracted():
    diff = '+    cur.execute("SELECT id, total FROM payments WHERE cart_id = %s", (cid,))\n'
    assert extract_tables_from_diff(diff) == ["payments"]


def test_create_table_is_extracted():
    diff = "+CREATE TABLE IF NOT EXISTS refunds (\n+    id UUID PRIMARY KEY\n+);\n"
    assert extract_tables_from_diff(diff) == ["refunds"]


def test_alter_table_is_extracted():
    diff = "+ALTER TABLE orders ADD COLUMN refunded_at TIMESTAMPTZ;\n"
    assert extract_tables_from_diff(diff) == ["orders"]


def test_table_names_are_deduplicated_across_statements_and_case():
    diff = """\
+    cur.execute("INSERT INTO orders (id) VALUES (%s)", (oid,))
+    cur.execute("UPDATE orders SET status = 'paid' WHERE id = %s", (oid,))
+    cur.execute("select total from Orders where id = %s", (oid,))
"""
    assert extract_tables_from_diff(diff) == ["orders"]


def test_multiple_distinct_tables_keep_first_seen_order():
    diff = """\
+    cur.execute("INSERT INTO carts (id) VALUES (%s)", (cid,))
+    cur.execute("SELECT * FROM payments WHERE cart_id = %s", (cid,))
+ALTER TABLE orders ADD COLUMN note TEXT;
"""
    assert extract_tables_from_diff(diff) == ["carts", "payments", "orders"]


def test_no_sql_in_diff_returns_empty_list():
    diff = """\
diff --git a/engine/comparator.py b/engine/comparator.py
@@ -1,3 +1,4 @@
+def compare(base, target):
+    return [d for d in base if d not in target]
"""
    assert extract_tables_from_diff(diff) == []


def test_python_import_is_not_mistaken_for_a_table():
    # A bare `from x import y` must not read as `SELECT ... FROM x`.
    diff = "+from engine.manifest import load_manifest\n+import structlog\n"
    assert extract_tables_from_diff(diff) == []


def test_quoted_table_name_is_unquoted():
    diff = '+    cur.execute(\'INSERT INTO "orders" (id) VALUES (%s)\', (oid,))\n'
    assert extract_tables_from_diff(diff) == ["orders"]


def test_removed_sql_lines_are_ignored():
    diff = """\
@@ -1,2 +1,2 @@
-    cur.execute("INSERT INTO legacy_orders (id) VALUES (%s)", (oid,))
+    cur.execute("INSERT INTO orders (id) VALUES (%s)", (oid,))
"""
    assert extract_tables_from_diff(diff) == ["orders"]
