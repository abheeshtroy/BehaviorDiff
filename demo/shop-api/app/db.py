"""Postgres access for the shop API.

Each version under comparison gets its own database, and BehaviorDiff passes
that version's dsn in DATABASE_URL — so nothing here is shared between base and
target, and a write by one version can never show up in the other's snapshot.
"""

from __future__ import annotations

import os

import psycopg

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/shop"
)


def connect() -> psycopg.Connection:
    """Open a connection. Used as a context manager, it commits on clean exit."""
    return psycopg.connect(DATABASE_URL)
