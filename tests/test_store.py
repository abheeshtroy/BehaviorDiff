"""Tests for web.store — SQLite persistence layer."""

import json
import tempfile
from pathlib import Path

import pytest

from web.store import save_run, list_runs, get_run


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
