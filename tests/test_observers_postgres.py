"""Unit tests for the Postgres observer.

diff() is tested purely in-memory against hand-built PostgresSnapshot
objects — no real Postgres connection is needed. snapshot() is tested by
mocking psycopg.connect, following the same pattern as test_orchestrator.py.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from engine.manifest import NormalizeConfig
from engine.observers import postgres as pg_module
from engine.observers.postgres import (
    PostgresDiff,
    PostgresObserver,
    PostgresObserverError,
    PostgresSnapshot,
)


def _snapshot(tables: dict[str, list[dict]], primary_keys: dict[str, list[str]]) -> PostgresSnapshot:
    return PostgresSnapshot(tables=tables, primary_keys=primary_keys)


def _delta(table: str, pk_columns: list[str], before_rows: list[dict], after_rows: list[dict]) -> PostgresDiff:
    """Build a one-version delta the way the CLI does: diff a table's own before/after snapshot."""
    before = _snapshot({table: before_rows}, {table: pk_columns})
    after = _snapshot({table: after_rows}, {table: pk_columns})
    return PostgresObserver.diff(before, after)


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


# -- compare_deltas(): delta-of-deltas across two separate databases ----------


def test_compare_deltas_identical_deltas_produce_no_findings() -> None:
    base_delta = _delta(
        "orders",
        ["id"],
        [{"id": 1, "status": "pending"}],
        [{"id": 1, "status": "shipped"}, {"id": 2, "status": "pending"}],
    )
    target_delta = _delta(
        "orders",
        ["id"],
        [{"id": 1, "status": "pending"}],
        [{"id": 1, "status": "shipped"}, {"id": 2, "status": "pending"}],
    )

    result = PostgresObserver.compare_deltas(base_delta, target_delta)

    assert result.tables["orders"].inserted == []
    assert result.tables["orders"].deleted == []
    assert result.tables["orders"].modified == []


def test_compare_deltas_insert_asymmetry_only_base_is_deleted_finding() -> None:
    base_delta = _delta("orders", ["id"], [], [{"id": 1, "status": "pending"}])
    target_delta = _delta("orders", ["id"], [], [])

    result = PostgresObserver.compare_deltas(base_delta, target_delta)

    assert result.tables["orders"].deleted == [{"id": 1, "status": "pending"}]
    assert result.tables["orders"].inserted == []
    assert result.tables["orders"].modified == []


def test_compare_deltas_insert_asymmetry_only_target_is_inserted_finding() -> None:
    base_delta = _delta("orders", ["id"], [], [])
    target_delta = _delta("orders", ["id"], [], [{"id": 1, "status": "pending"}])

    result = PostgresObserver.compare_deltas(base_delta, target_delta)

    assert result.tables["orders"].inserted == [{"id": 1, "status": "pending"}]
    assert result.tables["orders"].deleted == []
    assert result.tables["orders"].modified == []


def test_compare_deltas_both_insert_same_pk_different_values_is_modified() -> None:
    base_delta = _delta("orders", ["id"], [], [{"id": 1, "status": "pending"}])
    target_delta = _delta("orders", ["id"], [], [{"id": 1, "status": "cancelled"}])

    result = PostgresObserver.compare_deltas(base_delta, target_delta)

    [modified] = result.tables["orders"].modified
    assert modified.primary_key == {"id": 1}
    assert modified.before == {"id": 1, "status": "pending"}
    assert modified.after == {"id": 1, "status": "cancelled"}
    assert result.tables["orders"].inserted == []
    assert result.tables["orders"].deleted == []


def test_compare_deltas_delete_asymmetry_only_base_is_inserted_finding() -> None:
    """Delete polarity is mirrored: only base deleting a row means it still exists in target's database."""
    base_delta = _delta("orders", ["id"], [{"id": 1, "status": "pending"}], [])
    target_delta = _delta("orders", ["id"], [{"id": 1, "status": "pending"}], [{"id": 1, "status": "pending"}])

    result = PostgresObserver.compare_deltas(base_delta, target_delta)

    assert result.tables["orders"].inserted == [{"id": 1, "status": "pending"}]
    assert result.tables["orders"].deleted == []
    assert result.tables["orders"].modified == []


def test_compare_deltas_delete_asymmetry_only_target_is_deleted_finding() -> None:
    base_delta = _delta("orders", ["id"], [{"id": 1, "status": "pending"}], [{"id": 1, "status": "pending"}])
    target_delta = _delta("orders", ["id"], [{"id": 1, "status": "pending"}], [])

    result = PostgresObserver.compare_deltas(base_delta, target_delta)

    assert result.tables["orders"].deleted == [{"id": 1, "status": "pending"}]
    assert result.tables["orders"].inserted == []
    assert result.tables["orders"].modified == []


def test_compare_deltas_modify_only_one_side_is_modified_finding() -> None:
    base_delta = _delta("orders", ["id"], [{"id": 1, "status": "pending"}], [{"id": 1, "status": "shipped"}])
    target_delta = _delta("orders", ["id"], [{"id": 1, "status": "pending"}], [{"id": 1, "status": "pending"}])

    result = PostgresObserver.compare_deltas(base_delta, target_delta)

    [modified] = result.tables["orders"].modified
    assert modified.primary_key == {"id": 1}
    assert modified.before == {"id": 1, "status": "shipped"}
    assert modified.after == {"id": 1, "status": "pending"}


def test_compare_deltas_modify_both_sides_compares_after_values() -> None:
    base_delta = _delta("orders", ["id"], [{"id": 1, "status": "pending"}], [{"id": 1, "status": "shipped"}])
    target_delta = _delta("orders", ["id"], [{"id": 1, "status": "pending"}], [{"id": 1, "status": "cancelled"}])

    result = PostgresObserver.compare_deltas(base_delta, target_delta)

    [modified] = result.tables["orders"].modified
    assert modified.primary_key == {"id": 1}
    assert modified.before == {"id": 1, "status": "shipped"}
    assert modified.after == {"id": 1, "status": "cancelled"}


def test_compare_deltas_mixed_scenario_across_multiple_categories() -> None:
    """Insert-only-target, delete-only-base, and an identical modify all land in one table diff."""
    base_delta = _delta(
        "orders",
        ["id"],
        [{"id": 1, "status": "pending"}, {"id": 2, "status": "pending"}],
        [{"id": 2, "status": "shipped"}],
    )
    target_delta = _delta(
        "orders",
        ["id"],
        [{"id": 1, "status": "pending"}, {"id": 2, "status": "pending"}],
        [{"id": 1, "status": "pending"}, {"id": 2, "status": "shipped"}, {"id": 3, "status": "pending"}],
    )

    result = PostgresObserver.compare_deltas(base_delta, target_delta)
    table = result.tables["orders"]

    # base deleted id=1, target kept it -> still exists on target's side -> "inserted"
    assert {"id": 1, "status": "pending"} in table.inserted
    # target inserted id=3, base never did -> genuinely new
    assert {"id": 3, "status": "pending"} in table.inserted
    assert len(table.inserted) == 2
    # both modified id=2 identically (pending -> shipped) -> cancels out
    assert table.deleted == []
    assert table.modified == []


def test_compare_deltas_identical_row_with_different_random_uuid_pk_produces_no_findings() -> None:
    """Reproduces the Scenario 3 bug: each version's server generates its own
    random UUID for a new row's primary key (and any foreign key), so the raw
    values never match across two separate databases even when the row is,
    behaviourally, identical."""
    base_delta = _delta(
        "orders",
        ["id"],
        [],
        [
            {
                "id": "11111111-1111-1111-1111-111111111111",
                "cart_id": "22222222-2222-2222-2222-222222222222",
                "total": 4500,
                "status": "confirmed",
            }
        ],
    )
    target_delta = _delta(
        "orders",
        ["id"],
        [],
        [
            {
                "id": "33333333-3333-3333-3333-333333333333",
                "cart_id": "44444444-4444-4444-4444-444444444444",
                "total": 4500,
                "status": "confirmed",
            }
        ],
    )

    result = PostgresObserver.compare_deltas(base_delta, target_delta, config=NormalizeConfig())

    assert result.tables["orders"].inserted == []
    assert result.tables["orders"].deleted == []
    assert result.tables["orders"].modified == []


def test_compare_deltas_random_uuid_pk_with_genuinely_different_content_is_modified() -> None:
    """Same random-pk situation, but the row really is different this time --
    should still surface as exactly one modified finding, not an add+delete."""
    base_delta = _delta(
        "orders",
        ["id"],
        [],
        [{"id": "11111111-1111-1111-1111-111111111111", "total": 4500, "status": "confirmed"}],
    )
    target_delta = _delta(
        "orders",
        ["id"],
        [],
        [{"id": "33333333-3333-3333-3333-333333333333", "total": 4500, "status": "cancelled"}],
    )

    result = PostgresObserver.compare_deltas(base_delta, target_delta, config=NormalizeConfig())

    [modified] = result.tables["orders"].modified
    assert modified.before["status"] == "confirmed"
    assert modified.after["status"] == "cancelled"
    assert result.tables["orders"].inserted == []
    assert result.tables["orders"].deleted == []


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
