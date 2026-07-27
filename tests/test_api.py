"""Tests for web.api — FastAPI endpoints."""

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from web.api import app
from web.store import save_run


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_runs.db"


@pytest.fixture()
def client(db_path):
    """TestClient that patches store functions to use a temp DB."""
    with patch("web.api.list_runs", wraps=lambda **kw: __import__("web.store", fromlist=["list_runs"]).list_runs(db_path=db_path, **kw)), \
         patch("web.api.get_run", wraps=lambda run_id, **kw: __import__("web.store", fromlist=["get_run"]).get_run(run_id, db_path=db_path)):
        yield TestClient(app), db_path


def _make_result():
    return {
        "findings": [
            {
                "category": "http",
                "workflow_name": "checkout",
                "step_index": 0,
                "summary": "POST /checkout: status 200 -> 400",
                "evidence_base": {},
                "evidence_target": {},
                "severity": "changed",
            }
        ],
        "noise_summary": {"http_suppressed": 1, "postgres_suppressed": 0},
        "metadata": {"total_workflows": 1, "total_steps": 2, "duration_seconds": 0.8},
    }


def test_health():
    tc = TestClient(app)
    resp = tc.get("/api/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_list_runs_empty(db_path):
    from web import store
    with patch.object(store, "_DEFAULT_DB_PATH", db_path):
        with patch("web.api.list_runs", side_effect=lambda **kw: store.list_runs(db_path=db_path, **kw)):
            tc = TestClient(app)
            resp = tc.get("/api/runs")
            assert resp.status_code == 200
            assert resp.json() == []


def test_list_and_get_run(db_path):
    from web import store
    run_id = save_run(
        manifest_path="m.yaml",
        app_name="shop",
        result=_make_result(),
        db_path=db_path,
    )

    with patch("web.api.list_runs", side_effect=lambda **kw: store.list_runs(db_path=db_path, **kw)), \
         patch("web.api.get_run", side_effect=lambda rid, **kw: store.get_run(rid, db_path=db_path)):
        tc = TestClient(app)

        resp = tc.get("/api/runs")
        assert resp.status_code == 200
        runs = resp.json()
        assert len(runs) == 1
        assert runs[0]["id"] == run_id
        assert runs[0]["app_name"] == "shop"

        resp = tc.get(f"/api/runs/{run_id}")
        assert resp.status_code == 200
        detail = resp.json()
        assert detail["total_findings"] == 1
        assert len(detail["result"]["findings"]) == 1


def test_get_run_not_found(db_path):
    from web import store
    with patch("web.api.get_run", side_effect=lambda rid, **kw: store.get_run(rid, db_path=db_path)):
        tc = TestClient(app)
        resp = tc.get("/api/runs/nonexistent")
        assert resp.status_code == 404
