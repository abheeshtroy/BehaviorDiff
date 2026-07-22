"""Unit tests for the Postgres observer.

diff() is tested purely in-memory against hand-built PostgresSnapshot
objects — no real Postgres connection is needed. snapshot() is tested by
mocking psycopg.connect, following the same pattern as test_orchestrator.py.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from engine.observers import postgres as pg_module
from engine.observers.postgres import (
    PostgresObserver,
    PostgresObserverError,
    PostgresSnapshot,
)


def _snapshot(tables: dict[str, list[dict]], primary_keys: dict[str, list[str]]) -> PostgresSnapshot:
    return PostgresSnapshot(tables=tables, primary_keys=primary_keys)


# -- diff(): inserted / deleted / modified -----------------------------------


def test_diff_detects_inserted_row() -> None:
    before = _snapshot({"orders": [{"id": 1, "status": "pending"}]}, {"orders": ["id"]})
    after = _snapshot(
        {"orders": [{"id": 1, "status": "pending"}, {"id": 2, "status": "pending"}]},
        {"orders": ["id"]},
    )

    diff = PostgresObserver.diff(before, after)

    assert diff.tables["orders"].inserted == [{"id": 2, "status": "pending"}]
    assert diff.tables["orders"].deleted == []
    assert diff.tables["orders"].modified == []


def test_diff_detects_deleted_row() -> None:
    before = _snapshot(
        {"orders": [{"id": 1, "status": "pending"}, {"id": 2, "status": "pending"}]},
        {"orders": ["id"]},
    )
    after = _snapshot({"orders": [{"id": 1, "status": "pending"}]}, {"orders": ["id"]})

    diff = PostgresObserver.diff(before, after)

    assert diff.tables["orders"].deleted == [{"id": 2, "status": "pending"}]
    assert diff.tables["orders"].inserted == []
    assert diff.tables["orders"].modified == []


def test_diff_detects_modified_row_with_old_and_new_values() -> None:
    before = _snapshot({"orders": [{"id": 1, "status": "pending"}]}, {"orders": ["id"]})
    after = _snapshot({"orders": [{"id": 1, "status": "shipped"}]}, {"orders": ["id"]})

    diff = PostgresObserver.diff(before, after)

    [modified] = diff.tables["orders"].modified
    assert modified.primary_key == {"id": 1}
    assert modified.before == {"id": 1, "status": "pending"}
    assert modified.after == {"id": 1, "status": "shipped"}


def test_diff_unchanged_row_produces_no_findings() -> None:
    row = {"id": 1, "status": "pending"}
    before = _snapshot({"orders": [row]}, {"orders": ["id"]})
    after = _snapshot({"orders": [dict(row)]}, {"orders": ["id"]})

    diff = PostgresObserver.diff(before, after)

    assert diff.tables["orders"].inserted == []
    assert diff.tables["orders"].deleted == []
    assert diff.tables["orders"].modified == []


def test_diff_handles_multiple_tables_independently() -> None:
    before = _snapshot(
        {
            "orders": [{"id": 1, "status": "pending"}],
            "payments": [{"id": "p1", "amount": 10}],
        },
        {"orders": ["id"], "payments": ["id"]},
    )
    after = _snapshot(
        {
            "orders": [{"id": 1, "status": "shipped"}],
            "payments": [{"id": "p1", "amount": 10}],
        },
        {"orders": ["id"], "payments": ["id"]},
    )

    diff = PostgresObserver.diff(before, after)

    assert len(diff.tables["orders"].modified) == 1
    assert diff.tables["payments"].modified == []


def test_diff_composite_primary_key() -> None:
    before = _snapshot(
        {"cart_items": [{"cart_id": 1, "sku": "SHOE-42", "qty": 1}]},
        {"cart_items": ["cart_id", "sku"]},
    )
    after = _snapshot(
        {"cart_items": [{"cart_id": 1, "sku": "SHOE-42", "qty": 2}]},
        {"cart_items": ["cart_id", "sku"]},
    )

    diff = PostgresObserver.diff(before, after)

    [modified] = diff.tables["cart_items"].modified
    assert modified.primary_key == {"cart_id": 1, "sku": "SHOE-42"}
    assert modified.before["qty"] == 1
    assert modified.after["qty"] == 2


# -- diff(): error cases -------------------------------------------------------


def test_diff_raises_when_tables_differ_between_snapshots() -> None:
    before = _snapshot({"orders": []}, {"orders": ["id"]})
    after = _snapshot({"payments": []}, {"payments": ["id"]})

    with pytest.raises(PostgresObserverError, match="different tables"):
        PostgresObserver.diff(before, after)


def test_diff_raises_when_primary_keys_differ_between_snapshots() -> None:
    before = _snapshot({"orders": []}, {"orders": ["id"]})
    after = _snapshot({"orders": []}, {"orders": ["order_id"]})

    with pytest.raises(PostgresObserverError, match="primary key columns differ"):
        PostgresObserver.diff(before, after)


def test_diff_raises_when_row_missing_primary_key_column() -> None:
    before = _snapshot({"orders": [{"status": "pending"}]}, {"orders": ["id"]})
    after = _snapshot({"orders": [{"status": "pending"}]}, {"orders": ["id"]})

    with pytest.raises(PostgresObserverError, match="orders"):
        PostgresObserver.diff(before, after)


# -- snapshot(): mocked psycopg connection -------------------------------------


def _mock_cursor(fetchall_result: list) -> MagicMock:
    cursor = MagicMock()
    cursor.__enter__.return_value = cursor
    cursor.__exit__.return_value = False
    cursor.fetchall.return_value = fetchall_result
    return cursor


def test_snapshot_reads_rows_and_primary_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    pk_cursor = _mock_cursor([("id",)])
    rows_cursor = _mock_cursor([{"id": 1, "status": "pending"}])

    conn = MagicMock()
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    conn.cursor.side_effect = [pk_cursor, rows_cursor]

    monkeypatch.setattr(pg_module.psycopg, "connect", MagicMock(return_value=conn))

    observer = PostgresObserver(dsn="postgresql://fake")
    snapshot = observer.snapshot(["orders"])

    assert snapshot.tables == {"orders": [{"id": 1, "status": "pending"}]}
    assert snapshot.primary_keys == {"orders": ["id"]}


def test_snapshot_raises_when_table_has_no_primary_key(monkeypatch: pytest.MonkeyPatch) -> None:
    pk_cursor = _mock_cursor([])

    conn = MagicMock()
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    conn.cursor.side_effect = [pk_cursor]

    monkeypatch.setattr(pg_module.psycopg, "connect", MagicMock(return_value=conn))

    observer = PostgresObserver(dsn="postgresql://fake")

    with pytest.raises(PostgresObserverError, match="no primary key"):
        observer.snapshot(["orders"])


def test_snapshot_wraps_psycopg_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = MagicMock()
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    conn.cursor.side_effect = pg_module.psycopg.OperationalError("connection refused")

    monkeypatch.setattr(pg_module.psycopg, "connect", MagicMock(return_value=conn))

    observer = PostgresObserver(dsn="postgresql://fake")

    with pytest.raises(PostgresObserverError, match="failed to snapshot"):
        observer.snapshot(["orders"])
