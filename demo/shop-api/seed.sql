CREATE TABLE IF NOT EXISTS carts (
    id TEXT PRIMARY KEY,
    items TEXT,
    total INTEGER NOT NULL,
    discount_code TEXT
);

CREATE TABLE IF NOT EXISTS orders (
    id TEXT PRIMARY KEY,
    cart_id TEXT REFERENCES carts(id),
    total INTEGER NOT NULL,
    status TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    order_id TEXT REFERENCES orders(id),
    type TEXT NOT NULL,
    status TEXT NOT NULL
);
