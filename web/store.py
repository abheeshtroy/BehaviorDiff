"""SQLite storage for BehaviorDiff run results."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger(__name__)

_DEFAULT_DB_PATH = Path.home() / ".behaviordiff" / "runs.db"

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    manifest_path TEXT NOT NULL,
    app_name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'completed',
    created_at TEXT NOT NULL,
    duration_seconds REAL,
    total_findings INTEGER NOT NULL DEFAULT 0,
    total_suppressed INTEGER NOT NULL DEFAULT 0,
    total_workflows INTEGER NOT NULL DEFAULT 0,
    total_steps INTEGER NOT NULL DEFAULT 0,
    result_json TEXT NOT NULL,
    intent_json TEXT,
    classification_json TEXT,
    events_json TEXT
);
"""

# Columns added after the first release. A database created before them exists
# in the wild (~/.behaviordiff/runs.db is never recreated), so every connection
# tries to add them and treats "already there" as success. Nullable by
# necessity: an existing row has no value to backfill.
_MIGRATIONS = (
    "ALTER TABLE runs ADD COLUMN events_json TEXT",
)


def _connect(db_path: Path | None = None) -> sqlite3.Connection:
    path = db_path or _DEFAULT_DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute(_SCHEMA)
    for statement in _MIGRATIONS:
        try:
            conn.execute(statement)
        except sqlite3.OperationalError as exc:
            # The only acceptable failure is the column already existing —
            # anything else is a real problem with the database, not a
            # migration that has already run.
            if "duplicate column name" not in str(exc):
                raise
    conn.commit()
    return conn


def save_run(
    *,
    manifest_path: str,
    app_name: str,
    result: dict[str, Any],
    intent: dict[str, Any] | None = None,
    classification: dict[str, Any] | None = None,
    events: list[dict[str, Any]] | None = None,
    db_path: Path | None = None,
) -> str:
    """Persist a completed run. Returns the generated run ID.

    events is the run's progress stream as the pipeline emitted it, kept so the
    detail view can reconstruct the run's shape in time after the live
    WebSocket is gone. Omitting it stores NULL; runs saved before the column
    existed read back as events=None.
    """
    run_id = uuid.uuid4().hex[:12]
    now = datetime.now(timezone.utc).isoformat()

    meta = result.get("metadata", {})
    findings = result.get("findings", [])
    noise = result.get("noise_summary", {})

    conn = _connect(db_path)
    try:
        conn.execute(
            """INSERT INTO runs
               (id, manifest_path, app_name, status, created_at,
                duration_seconds, total_findings, total_suppressed,
                total_workflows, total_steps, result_json,
                intent_json, classification_json, events_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                run_id,
                manifest_path,
                app_name,
                "completed",
                now,
                meta.get("duration_seconds"),
                len(findings),
                noise.get("http_suppressed", 0) + noise.get("postgres_suppressed", 0),
                meta.get("total_workflows", 0),
                meta.get("total_steps", 0),
                json.dumps(result),
                json.dumps(intent) if intent else None,
                json.dumps(classification) if classification else None,
                json.dumps(events) if events is not None else None,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    log.info("run_saved", run_id=run_id)
    return run_id


def list_runs(*, db_path: Path | None = None, limit: int = 50) -> list[dict[str, Any]]:
    """Return recent runs (without full result JSON) for the listing view."""
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            """SELECT id, manifest_path, app_name, status, created_at,
                      duration_seconds, total_findings, total_suppressed,
                      total_workflows, total_steps
               FROM runs ORDER BY created_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def get_run(run_id: str, *, db_path: Path | None = None) -> dict[str, Any] | None:
    """Return a single run with parsed result, intent, classification, events.

    Every key is always present. "events" is None for a run stored before the
    stream was persisted — the caller must be able to tell "no events recorded"
    from "a run with no events", and a missing key can say neither.
    """
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM runs WHERE id = ?", (run_id,)
        ).fetchone()
        if row is None:
            return None
        data = dict(row)
        data["result"] = json.loads(data.pop("result_json"))
        intent_raw = data.pop("intent_json")
        class_raw = data.pop("classification_json")
        events_raw = data.pop("events_json", None)
        data["intent"] = json.loads(intent_raw) if intent_raw else None
        data["classification"] = json.loads(class_raw) if class_raw else None
        data["events"] = json.loads(events_raw) if events_raw else None
        return data
    finally:
        conn.close()
