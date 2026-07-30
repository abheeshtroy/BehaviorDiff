"""Tests for web.store — SQLite persistence layer."""

import json
import sqlite3
import tempfile
from pathlib import Path

import pytest

from web.store import _connect, save_run, list_runs, get_run


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_runs.db"


def _make_result(**overrides):
    base = {
        "findings": [
            {
                "category": "http",
                "workflow_name": "checkout",
                "step_index": 0,
                "summary": "POST /checkout: status 200 -> 400",
                "evidence_base": {"status_code": 200, "body": {}},
                "evidence_target": {"status_code": 400, "body": {}},
                "severity": "changed",
            }
        ],
        "noise_summary": {"http_suppressed": 2, "postgres_suppressed": 0},
        "metadata": {
            "total_workflows": 1,
            "total_steps": 3,
            "duration_seconds": 1.5,
        },
    }
    base.update(overrides)
    return base


def test_save_and_get(db_path):
    result = _make_result()
    run_id = save_run(
        manifest_path="demo/manifest.yaml",
        app_name="shop-api",
        result=result,
        db_path=db_path,
    )
    assert len(run_id) == 12

    run = get_run(run_id, db_path=db_path)
    assert run is not None
    assert run["app_name"] == "shop-api"
    assert run["total_findings"] == 1
    assert run["total_suppressed"] == 2
    assert run["result"]["findings"][0]["summary"] == "POST /checkout: status 200 -> 400"
    assert run["intent"] is None
    assert run["classification"] is None


def test_save_with_intent_and_classification(db_path):
    result = _make_result()
    intent = {"summary": "validation fix", "changed_routes": ["/checkout"]}
    classification = {"classifications": [], "summary": "all intended"}

    run_id = save_run(
        manifest_path="m.yaml",
        app_name="app",
        result=result,
        intent=intent,
        classification=classification,
        db_path=db_path,
    )

    run = get_run(run_id, db_path=db_path)
    assert run["intent"]["summary"] == "validation fix"
    assert run["classification"]["summary"] == "all intended"


def test_list_runs_ordering(db_path):
    for i in range(5):
        save_run(
            manifest_path=f"m{i}.yaml",
            app_name=f"app-{i}",
            result=_make_result(),
            db_path=db_path,
        )

    runs = list_runs(db_path=db_path)
    assert len(runs) == 5
    # Most recent first
    assert runs[0]["app_name"] == "app-4"
    assert runs[4]["app_name"] == "app-0"
    # Listing doesn't include full result JSON
    assert "result_json" not in runs[0]
    assert "result" not in runs[0]


def test_list_runs_limit(db_path):
    for i in range(10):
        save_run(
            manifest_path="m.yaml",
            app_name="app",
            result=_make_result(),
            db_path=db_path,
        )

    runs = list_runs(db_path=db_path, limit=3)
    assert len(runs) == 3


def test_get_nonexistent(db_path):
    assert get_run("doesnotexist", db_path=db_path) is None


def _events():
    return [
        {"stage": "environments_starting", "message": "starting", "timestamp": 1.0, "data": {"direct": False}},
        {"stage": "workflow_started", "message": "running checkout", "timestamp": 2.0,
         "data": {"name": "checkout", "index": 0, "total": 1}},
        {"stage": "persisting", "message": "Saving the run", "timestamp": 3.0, "data": None},
    ]


def test_save_with_events(db_path):
    run_id = save_run(
        manifest_path="m.yaml",
        app_name="app",
        result=_make_result(),
        events=_events(),
        db_path=db_path,
    )

    run = get_run(run_id, db_path=db_path)
    assert run["events"] == _events()
    assert [e["stage"] for e in run["events"]] == [
        "environments_starting",
        "workflow_started",
        "persisting",
    ]
    assert run["events"][1]["data"]["name"] == "checkout"


def test_save_without_events_stores_null(db_path):
    run_id = save_run(
        manifest_path="m.yaml",
        app_name="app",
        result=_make_result(),
        db_path=db_path,
    )

    conn = _connect(db_path)
    try:
        stored = conn.execute("SELECT events_json FROM runs WHERE id = ?", (run_id,)).fetchone()
    finally:
        conn.close()
    assert stored["events_json"] is None

    # The key is always present, so a caller can tell "nothing recorded" from
    # "a run with no events" instead of guessing at a missing key.
    run = get_run(run_id, db_path=db_path)
    assert "events" in run
    assert run["events"] is None


def test_an_empty_event_list_round_trips_as_a_list(db_path):
    run_id = save_run(
        manifest_path="m.yaml",
        app_name="app",
        result=_make_result(),
        events=[],
        db_path=db_path,
    )

    # "[]" is falsy JSON but a real answer: the run recorded a stream, and it
    # was empty. It must not decay into None.
    assert get_run(run_id, db_path=db_path)["events"] == []


def test_a_fresh_database_gets_the_events_column(db_path):
    conn = _connect(db_path)
    try:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(runs)")}
    finally:
        conn.close()
    assert "events_json" in columns


def test_a_database_predating_the_events_column_is_migrated(db_path):
    # A schema as it shipped before events were persisted.
    legacy = sqlite3.connect(str(db_path))
    legacy.execute(
        """CREATE TABLE runs (
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
               classification_json TEXT
           )"""
    )
    legacy.execute(
        """INSERT INTO runs (id, manifest_path, app_name, created_at, result_json)
           VALUES ('old123456789', 'old.yaml', 'legacy-app', '2026-01-01T00:00:00+00:00', ?)""",
        (json.dumps(_make_result()),),
    )
    legacy.commit()
    legacy.close()

    # Connecting adds the column, so the pre-existing row reads back with
    # events=None rather than blowing up on a missing column.
    old = get_run("old123456789", db_path=db_path)
    assert old["app_name"] == "legacy-app"
    assert old["events"] is None

    # And the migrated database can store events from then on.
    run_id = save_run(
        manifest_path="m.yaml",
        app_name="app",
        result=_make_result(),
        events=_events(),
        db_path=db_path,
    )
    assert get_run(run_id, db_path=db_path)["events"] == _events()


def test_migration_is_idempotent_across_connections(db_path):
    for _ in range(3):
        _connect(db_path).close()

    run_id = save_run(
        manifest_path="m.yaml",
        app_name="app",
        result=_make_result(),
        events=_events(),
        db_path=db_path,
    )
    assert get_run(run_id, db_path=db_path)["events"] == _events()


def test_no_findings(db_path):
    result = _make_result(findings=[])
    run_id = save_run(
        manifest_path="m.yaml",
        app_name="app",
        result=result,
        db_path=db_path,
    )
    run = get_run(run_id, db_path=db_path)
    assert run["total_findings"] == 0
