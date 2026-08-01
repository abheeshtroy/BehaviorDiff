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

from engine.manifest import NormalizeConfig
from engine.normalizer import Normalizer

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
    primary_keys: dict[str, list[str]] = Field(default_factory=dict)


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
        primary_keys: dict[str, list[str]] = {}
        for table in sorted(before_tables):
            pk_columns = before.primary_keys.get(table)
            if pk_columns != after.primary_keys.get(table):
                raise PostgresObserverError(
                    f"table {table!r}: primary key columns differ between snapshots "
                    f"(before={pk_columns}, after={after.primary_keys.get(table)})"
                )
            if not pk_columns:
                raise PostgresObserverError(f"table {table!r}: snapshot has no primary key columns recorded")

            primary_keys[table] = pk_columns
            tables[table] = PostgresObserver._diff_table(
                table, pk_columns, before.tables[table], after.tables[table]
            )

        return PostgresDiff(tables=tables, primary_keys=primary_keys)

    @staticmethod
    def compare_deltas(
        base_delta: PostgresDiff, target_delta: PostgresDiff, config: NormalizeConfig | None = None
    ) -> PostgresDiff:
        """Compare what changed in base's own database against what changed in target's own database.

        With separate databases per version, comparing final states directly
        is meaningless — the rows live in different databases entirely. What
        we can compare is each version's *delta*: what it inserted, deleted,
        and modified relative to its own identical starting seed. A row both
        versions inserted/modified/deleted identically cancels out; only
        genuine behavioral divergence between the two deltas remains.

        Per table, inserts and deletes are compared by primary key:
          - present in only one side's insert set -> an added/removed finding
          - inserted by both with different values -> a modified finding
        Deletes mirror this with the polarity flipped, since "only base
        deleted this row" means the row still exists in target's database.
        Modifies are compared similarly: rows both sides modified are
        compared by their resulting (after) values; a row only one side
        modified is reported directly, since the other side's current value
        is whatever it started as.

        Rows are paired across sides by *normalized* primary key rather than
        raw primary key: a primary key that's a UUID generated independently
        by each version's own server (the common case) never matches
        literally across two separate databases, even for a behaviorally
        identical row. Normalizing first reuses the same positional
        placeholder logic already used for HTTP bodies.
        """
        config = config or NormalizeConfig()
        table_names = sorted(set(base_delta.tables) | set(target_delta.tables))
        result_tables: dict[str, TableDiff] = {}
        primary_keys: dict[str, list[str]] = {}

        for table in table_names:
            base_pk = base_delta.primary_keys.get(table)
            target_pk = target_delta.primary_keys.get(table)
            pk_columns = base_pk or target_pk
            if base_pk is not None and target_pk is not None and base_pk != target_pk:
                raise PostgresObserverError(
                    f"table {table!r}: primary key columns differ between deltas "
                    f"(base={base_pk}, target={target_pk})"
                )
            if not pk_columns:
                raise PostgresObserverError(f"table {table!r}: no primary key columns available to compare deltas")

            primary_keys[table] = pk_columns
            result_tables[table] = PostgresObserver._compare_table_deltas(
                table,
                pk_columns,
                base_delta.tables.get(table, TableDiff()),
                target_delta.tables.get(table, TableDiff()),
                config,
            )

        return PostgresDiff(tables=result_tables, primary_keys=primary_keys)

    @staticmethod
    def _compare_table_deltas(
        table: str, pk_columns: list[str], base_diff: TableDiff, target_diff: TableDiff, config: NormalizeConfig
    ) -> TableDiff:
        base_normalizer = Normalizer(config)
        target_normalizer = Normalizer(config)

        inserted: list[dict[str, Any]] = []
        deleted: list[dict[str, Any]] = []
        modified: list[RowChange] = []

        # -- inserts: only base -> deleted finding, only target -> inserted finding,
        # both with the same normalized pk but different normalized values -> modified finding.
        paired, only_base, only_target = PostgresObserver._pair_rows_by_normalized_pk(
            base_diff.inserted, target_diff.inserted, pk_columns, base_normalizer, target_normalizer
        )
        deleted.extend(only_base)
        inserted.extend(only_target)
        for base_row, base_normalized, target_row, target_normalized in paired:
            if base_normalized != target_normalized:
                modified.append(
                    RowChange(
                        primary_key=dict(
                            zip(pk_columns, PostgresObserver._pk_tuple(pk_columns, table, base_row), strict=True)
                        ),
                        before=base_row,
                        after=target_row,
                    )
                )

        # -- deletes: mirrored polarity, since "only base deleted this row" means
        # target's database still has it.
        paired, only_base, only_target = PostgresObserver._pair_rows_by_normalized_pk(
            base_diff.deleted, target_diff.deleted, pk_columns, base_normalizer, target_normalizer
        )
        inserted.extend(only_base)
        deleted.extend(only_target)
        for base_row, base_normalized, target_row, target_normalized in paired:
            if base_normalized != target_normalized:
                modified.append(
                    RowChange(
                        primary_key=dict(
                            zip(pk_columns, PostgresObserver._pk_tuple(pk_columns, table, base_row), strict=True)
                        ),
                        before=base_row,
                        after=target_row,
                    )
                )

        # -- modifies: both -> compare resulting (after) values, only one side ->
        # report directly (the other side's current value is whatever it started as).
        paired_changes, only_base_changes, only_target_changes = PostgresObserver._pair_changes_by_normalized_pk(
            base_diff.modified, target_diff.modified, pk_columns, base_normalizer, target_normalizer
        )
        for change in only_base_changes:
            modified.append(RowChange(primary_key=change.primary_key, before=change.after, after=change.before))
        for change in only_target_changes:
            modified.append(RowChange(primary_key=change.primary_key, before=change.before, after=change.after))
        for base_change, base_after_normalized, target_change, target_after_normalized in paired_changes:
            if base_after_normalized != target_after_normalized:
                modified.append(
                    RowChange(
                        primary_key=base_change.primary_key, before=base_change.after, after=target_change.after
                    )
                )

        def row_sort_key(row: dict[str, Any]) -> tuple[str, ...]:
            return _pk_sort_key(PostgresObserver._pk_tuple(pk_columns, table, row))

        def change_sort_key(change: RowChange) -> tuple[str, ...]:
            return _pk_sort_key(tuple(change.primary_key[col] for col in pk_columns))

        return TableDiff(
            inserted=sorted(inserted, key=row_sort_key),
            deleted=sorted(deleted, key=row_sort_key),
            modified=sorted(modified, key=change_sort_key),
        )

    @staticmethod
    def _pair_rows_by_normalized_pk(
        base_rows: list[dict[str, Any]],
        target_rows: list[dict[str, Any]],
        pk_columns: list[str],
        base_normalizer: Normalizer,
        target_normalizer: Normalizer,
    ) -> tuple[
        list[tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]],
        list[dict[str, Any]],
        list[dict[str, Any]],
    ]:
        """Pair rows across two sides by normalized primary key, not raw primary key.

        Returns (paired, only_base, only_target). Each paired entry carries
        both raw rows plus both normalized rows: the caller builds a Finding
        from the raw data while deciding "changed or not" from the normalized
        data. Rows are consumed in list order, so ties pair up positionally.
        """
        target_by_key: dict[tuple[Any, ...], list[tuple[dict[str, Any], dict[str, Any]]]] = {}
        for row in target_rows:
            normalized, _ = target_normalizer.normalize(row)
            key = tuple(normalized[col] for col in pk_columns)
            target_by_key.setdefault(key, []).append((row, normalized))

        paired: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]] = []
        only_base: list[dict[str, Any]] = []

        for row in base_rows:
            normalized, _ = base_normalizer.normalize(row)
            key = tuple(normalized[col] for col in pk_columns)
            bucket = target_by_key.get(key)
            if bucket:
                target_row, target_normalized = bucket.pop(0)
                paired.append((row, normalized, target_row, target_normalized))
            else:
                only_base.append(row)

        only_target = [row for bucket in target_by_key.values() for row, _ in bucket]
        return paired, only_base, only_target

    @staticmethod
    def _pair_changes_by_normalized_pk(
        base_changes: list[RowChange],
        target_changes: list[RowChange],
        pk_columns: list[str],
        base_normalizer: Normalizer,
        target_normalizer: Normalizer,
    ) -> tuple[list[tuple[RowChange, dict[str, Any], RowChange, dict[str, Any]]], list[RowChange], list[RowChange]]:
        """Same pairing strategy as _pair_rows_by_normalized_pk, for RowChange objects."""

        def normalized_pk_and_after(
            change: RowChange, normalizer: Normalizer
        ) -> tuple[tuple[Any, ...], dict[str, Any]]:
            normalized_pk_row, _ = normalizer.normalize(change.primary_key)
            normalized_after, _ = normalizer.normalize(change.after)
            key = tuple(normalized_pk_row[col] for col in pk_columns)
            return key, normalized_after

        target_by_key: dict[tuple[Any, ...], list[tuple[RowChange, dict[str, Any]]]] = {}
        for change in target_changes:
            key, normalized_after = normalized_pk_and_after(change, target_normalizer)
            target_by_key.setdefault(key, []).append((change, normalized_after))

        paired: list[tuple[RowChange, dict[str, Any], RowChange, dict[str, Any]]] = []
        only_base: list[RowChange] = []

        for change in base_changes:
            key, normalized_after = normalized_pk_and_after(change, base_normalizer)
            bucket = target_by_key.get(key)
            if bucket:
                target_change, target_after = bucket.pop(0)
                paired.append((change, normalized_after, target_change, target_after))
            else:
                only_base.append(change)

        only_target = [change for bucket in target_by_key.values() for change, _ in bucket]
        return paired, only_base, only_target

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
