"""Postgres observer: snapshots selected tables and diffs them row-by-row.

A snapshot is a raw ``SELECT *`` of each observed table, plus the primary
key columns discovered from information_schema. Diffing is a pure function
over two snapshots — it never touches the database, so before/after
snapshots can be compared without holding a connection open, and the diff
logic can be tested without a live Postgres instance.
"""

from __future__ import annotations

from typing import Any

import psycopg
import structlog
from psycopg import sql
from psycopg.rows import dict_row
from pydantic import BaseModel, Field

log = structlog.get_logger(__name__)


def _pk_sort_key(pk: tuple[Any, ...]) -> tuple[str, ...]:
    return tuple(str(part) for part in pk)


class PostgresObserverError(Exception):
    """Raised when a table can't be snapshotted or diffed."""


class PostgresSnapshot(BaseModel):
    """Row state of observed tables, plus the primary key columns used to match rows across snapshots."""

    tables: dict[str, list[dict[str, Any]]]
    primary_keys: dict[str, list[str]]


class RowChange(BaseModel):
    """A single row whose values differ between two snapshots, keyed by primary key."""

    primary_key: dict[str, Any]
    before: dict[str, Any]
    after: dict[str, Any]


class TableDiff(BaseModel):
    """What changed in one table between two snapshots."""

    inserted: list[dict[str, Any]] = Field(default_factory=list)
    deleted: list[dict[str, Any]] = Field(default_factory=list)
    modified: list[RowChange] = Field(default_factory=list)


class PostgresDiff(BaseModel):
    """What changed across all observed tables between two snapshots."""

    tables: dict[str, TableDiff]


class PostgresObserver:
    """Snapshots and diffs selected Postgres tables for a comparison run."""

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    def snapshot(self, tables: list[str]) -> PostgresSnapshot:
        """Read all rows from the given tables, keyed by table name.

        Raises PostgresObserverError if a table can't be read or has no
        primary key, since row-by-row diffing depends on one.
        """
        table_rows: dict[str, list[dict[str, Any]]] = {}
        primary_keys: dict[str, list[str]] = {}
        try:
            with psycopg.connect(self.dsn) as conn:
                for table in tables:
                    pk_columns = self._primary_key_columns(conn, table)
                    if not pk_columns:
                        raise PostgresObserverError(
                            f"table {table!r} has no primary key; row-by-row diffing requires one"
                        )
                    primary_keys[table] = pk_columns
                    table_rows[table] = self._fetch_rows(conn, table)
        except psycopg.Error as exc:
            raise PostgresObserverError(f"failed to snapshot tables {tables}: {exc}") from exc

        log.info("snapshot taken", tables=tables, row_counts={t: len(r) for t, r in table_rows.items()})
        return PostgresSnapshot(tables=table_rows, primary_keys=primary_keys)

    @staticmethod
    def diff(before: PostgresSnapshot, after: PostgresSnapshot) -> PostgresDiff:
        """Compare two snapshots and return inserted, deleted, and modified rows per table.

        Pure function over already-captured snapshot data — never touches
        the database.
        """
        before_tables = set(before.tables)
        after_tables = set(after.tables)
        if before_tables != after_tables:
            raise PostgresObserverError(
                f"snapshots observe different tables: before={sorted(before_tables)} "
                f"after={sorted(after_tables)}"
            )

        tables: dict[str, TableDiff] = {}
        for table in sorted(before_tables):
            pk_columns = before.primary_keys.get(table)
            if pk_columns != after.primary_keys.get(table):
                raise PostgresObserverError(
                    f"table {table!r}: primary key columns differ between snapshots "
                    f"(before={pk_columns}, after={after.primary_keys.get(table)})"
                )
            if not pk_columns:
                raise PostgresObserverError(f"table {table!r}: snapshot has no primary key columns recorded")

            tables[table] = PostgresObserver._diff_table(
                table, pk_columns, before.tables[table], after.tables[table]
            )

        return PostgresDiff(tables=tables)

    @staticmethod
    def _diff_table(
        table: str, pk_columns: list[str], before_rows: list[dict[str, Any]], after_rows: list[dict[str, Any]]
    ) -> TableDiff:
        before_by_pk = {PostgresObserver._pk_tuple(pk_columns, table, row): row for row in before_rows}
        after_by_pk = {PostgresObserver._pk_tuple(pk_columns, table, row): row for row in after_rows}

        inserted_pks = sorted(after_by_pk.keys() - before_by_pk.keys(), key=_pk_sort_key)
        deleted_pks = sorted(before_by_pk.keys() - after_by_pk.keys(), key=_pk_sort_key)
        common_pks = sorted(before_by_pk.keys() & after_by_pk.keys(), key=_pk_sort_key)

        inserted = [after_by_pk[pk] for pk in inserted_pks]
        deleted = [before_by_pk[pk] for pk in deleted_pks]
        modified = [
            RowChange(
                primary_key=dict(zip(pk_columns, pk, strict=True)),
                before=before_by_pk[pk],
                after=after_by_pk[pk],
            )
            for pk in common_pks
            if before_by_pk[pk] != after_by_pk[pk]
        ]
        return TableDiff(inserted=inserted, deleted=deleted, modified=modified)

    @staticmethod
    def _pk_tuple(pk_columns: list[str], table: str, row: dict[str, Any]) -> tuple[Any, ...]:
        try:
            return tuple(row[col] for col in pk_columns)
        except KeyError as exc:
            raise PostgresObserverError(f"table {table!r}: row missing primary key column {exc}: {row}") from exc

    def _primary_key_columns(self, conn: psycopg.Connection, table: str) -> list[str]:
        query = """
            SELECT kcu.column_name
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
              ON tc.constraint_name = kcu.constraint_name
             AND tc.table_schema = kcu.table_schema
            WHERE tc.constraint_type = 'PRIMARY KEY'
              AND tc.table_name = %s
            ORDER BY kcu.ordinal_position
        """
        with conn.cursor() as cur:
            cur.execute(query, (table,))
            return [row[0] for row in cur.fetchall()]

    def _fetch_rows(self, conn: psycopg.Connection, table: str) -> list[dict[str, Any]]:
        query = sql.SQL("SELECT * FROM {}").format(sql.Identifier(table))
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(query)
            return cur.fetchall()
