"""Tests for web.api — FastAPI endpoints."""

import asyncio
import json
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi import WebSocketDisconnect
from fastapi.testclient import TestClient

from engine.pipeline import RunEvent
from web import run_registry
from web.api import WS_UNKNOWN_RUN, app
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


def test_get_run_carries_the_persisted_event_stream(db_path):
    from web import store
    events = [
        {"stage": "environments_starting", "message": "starting", "timestamp": 1.0, "data": {"direct": False}},
        {"stage": "persisting", "message": "Saving the run", "timestamp": 2.0, "data": None},
    ]
    with_events = save_run(
        manifest_path="m.yaml", app_name="shop", result=_make_result(), events=events, db_path=db_path
    )
    without_events = save_run(
        manifest_path="m.yaml", app_name="shop", result=_make_result(), db_path=db_path
    )

    with patch("web.api.get_run", side_effect=lambda rid, **kw: store.get_run(rid, db_path=db_path)):
        tc = TestClient(app)

        detail = tc.get(f"/api/runs/{with_events}").json()
        assert detail["events"] == events

        # A run stored before events were persisted answers with an explicit
        # null — the key is never simply absent, so the client can tell
        # "nothing was recorded" from "a field I forgot to read".
        old = tc.get(f"/api/runs/{without_events}").json()
        assert "events" in old
        assert old["events"] is None


def test_get_run_not_found(db_path):
    from web import store
    with patch("web.api.get_run", side_effect=lambda rid, **kw: store.get_run(rid, db_path=db_path)):
        tc = TestClient(app)
        resp = tc.get("/api/runs/nonexistent")
        assert resp.status_code == 404


# -- manifest discovery ------------------------------------------------------

DEMO_MANIFEST = "demo/manifests/scenario1-checkout-validation.yaml"


@pytest.fixture(autouse=True)
def clean_registry():
    """Keep live-run state from leaking between tests."""
    run_registry.clear()
    yield
    run_registry.clear()


def _event(stage: str, message: str = "", data: dict | None = None) -> RunEvent:
    return RunEvent(stage=stage, message=message or stage, timestamp=time.time(), data=data)


def _canned_pipeline(*events: RunEvent) -> MagicMock:
    """A run_pipeline stand-in that yields the given events and stops."""

    def fake_run_pipeline(manifest, **kwargs):
        yield from events

    return MagicMock(side_effect=fake_run_pipeline)


def test_list_manifests_returns_the_demo_manifests_with_metadata():
    tc = TestClient(app)
    resp = tc.get("/api/manifests")
    assert resp.status_code == 200

    manifests = resp.json()
    assert len(manifests) == 3

    by_filename = {entry["filename"]: entry for entry in manifests}
    assert set(by_filename) == {
        "scenario1-checkout-validation.yaml",
        "scenario2-retry-logic.yaml",
        "scenario3-response-cleanup.yaml",
    }

    scenario1 = by_filename["scenario1-checkout-validation.yaml"]
    assert scenario1["path"] == DEMO_MANIFEST
    assert scenario1["app_name"] == "shop-api"
    assert scenario1["workflow_count"] >= 1
    assert scenario1["error"] is None


def test_list_manifests_honours_the_manifest_dir_env_var(tmp_path, monkeypatch):
    manifest = tmp_path / "custom.yaml"
    manifest.write_text(
        "app:\n"
        "  name: other-app\n"
        "  start: uvicorn app.main:app\n"
        "  port: 8000\n"
        "  healthcheck: /health\n"
        "compare:\n"
        "  base_ref: main\n"
        "  target_ref: fix/thing\n"
        "  repo: .\n"
        "workflows:\n"
        "  - name: smoke\n"
        "    steps:\n"
        "      - method: GET\n"
        "        path: /health\n"
    )
    monkeypatch.setenv("BEHAVIORDIFF_MANIFEST_DIR", str(tmp_path))

    resp = TestClient(app).get("/api/manifests")
    assert resp.status_code == 200
    assert [(e["app_name"], e["workflow_count"]) for e in resp.json()] == [("other-app", 1)]


def test_an_unparseable_manifest_is_listed_but_does_not_break_the_endpoint(tmp_path, monkeypatch):
    (tmp_path / "broken.yaml").write_text("app:\n  name: [unclosed\n")
    (tmp_path / "fine.yaml").write_text(
        "app:\n"
        "  name: fine-app\n"
        "  start: uvicorn app.main:app\n"
        "  port: 8000\n"
        "  healthcheck: /health\n"
        "compare:\n"
        "  base_ref: main\n"
        "  target_ref: fix/thing\n"
        "  repo: .\n"
        "workflows:\n"
        "  - name: smoke\n"
        "    steps:\n"
        "      - method: GET\n"
        "        path: /health\n"
    )
    monkeypatch.setenv("BEHAVIORDIFF_MANIFEST_DIR", str(tmp_path))

    resp = TestClient(app).get("/api/manifests")
    assert resp.status_code == 200

    entries = {e["filename"]: e for e in resp.json()}
    assert entries["fine.yaml"]["app_name"] == "fine-app"
    assert entries["broken.yaml"]["app_name"] is None
    assert entries["broken.yaml"]["error"]


def test_list_manifests_is_empty_when_the_directory_is_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("BEHAVIORDIFF_MANIFEST_DIR", str(tmp_path / "does-not-exist"))
    resp = TestClient(app).get("/api/manifests")
    assert resp.status_code == 200
    assert resp.json() == []


# -- triggering a run --------------------------------------------------------


def test_trigger_returns_a_run_id_for_a_discovered_manifest():
    with patch.object(run_registry, "run_pipeline", _canned_pipeline(_event("done"))):
        resp = TestClient(app).post("/api/runs/trigger", json={"manifest_path": DEMO_MANIFEST})
        assert resp.status_code == 200

        run_id = resp.json()["run_id"]
        assert run_id
        assert run_registry.get_queue(run_id) is not None


def test_trigger_rejects_a_manifest_outside_the_manifest_directory(tmp_path):
    outside = tmp_path / "evil.yaml"
    outside.write_text("app: {}\n")

    resp = TestClient(app).post("/api/runs/trigger", json={"manifest_path": str(outside)})
    assert resp.status_code == 403
    assert run_registry.list_active_runs() == []


@pytest.mark.parametrize(
    "path",
    [
        "/etc/passwd",
        "demo/manifests/../../cli.py",
        "demo/manifests/nonexistent.yaml",
        "",
    ],
)
def test_trigger_rejects_arbitrary_paths(path):
    resp = TestClient(app).post("/api/runs/trigger", json={"manifest_path": path})
    assert resp.status_code == 403
    assert run_registry.list_active_runs() == []


def test_trigger_requires_a_manifest_path():
    resp = TestClient(app).post("/api/runs/trigger", json={})
    assert resp.status_code == 422


# -- streaming a run ---------------------------------------------------------


def test_the_stream_delivers_a_runs_events_in_order_and_closes_after_done():
    events = _canned_pipeline(
        _event("environments_starting", "Building and starting both versions", {"direct": False}),
        _event("environments_ready", "Environments ready", {"base_url": "http://base"}),
        _event("workflows_complete", "Ran 1 workflow(s)", {"count": 1}),
        _event("done", "Run complete: 1 finding(s)", {"run_id": "run-123"}),
    )

    with patch.object(run_registry, "run_pipeline", events):
        tc = TestClient(app)
        run_id = tc.post("/api/runs/trigger", json={"manifest_path": DEMO_MANIFEST}).json()["run_id"]

        received = []
        with tc.websocket_connect(f"/api/runs/{run_id}/stream") as websocket:
            for _ in range(4):
                received.append(websocket.receive_json())
            with pytest.raises(WebSocketDisconnect):
                websocket.receive_json()

    assert [event["stage"] for event in received] == [
        "environments_starting",
        "environments_ready",
        "workflows_complete",
        "done",
    ]
    assert received[1]["data"] == {"base_url": "http://base"}
    assert received[-1]["message"] == "Run complete: 1 finding(s)"
    assert all(event["timestamp"] > 0 for event in received)
    assert run_registry.get_status(run_id) == "done"


def test_the_stream_delivers_a_failed_runs_error_event():
    events = _canned_pipeline(
        _event("environments_starting", "Building and starting both versions"),
        _event(
            "error",
            "seed file not found: seed.sql",
            {
                "error": "seed file not found: seed.sql",
                "error_type": "OrchestratorError",
                "expected": True,
                "exception": RuntimeError("not serializable"),
            },
        ),
    )

    with patch.object(run_registry, "run_pipeline", events):
        tc = TestClient(app)
        run_id = tc.post("/api/runs/trigger", json={"manifest_path": DEMO_MANIFEST}).json()["run_id"]

        received = []
        with tc.websocket_connect(f"/api/runs/{run_id}/stream") as websocket:
            for _ in range(2):
                received.append(websocket.receive_json())
            with pytest.raises(WebSocketDisconnect):
                websocket.receive_json()

    assert [event["stage"] for event in received] == ["environments_starting", "error"]
    assert received[-1]["data"]["error_type"] == "OrchestratorError"
    # The live exception object can't cross the socket and must be dropped.
    assert "exception" not in received[-1]["data"]
    assert run_registry.get_status(run_id) == "error"


def test_the_stream_drops_the_in_process_only_payload_from_done():
    done = _event(
        "done",
        "Run complete",
        {
            "result": {"findings": []},
            "run_id": "run-123",
            "models": {"result": object()},
        },
    )

    with patch.object(run_registry, "run_pipeline", _canned_pipeline(done)):
        tc = TestClient(app)
        run_id = tc.post("/api/runs/trigger", json={"manifest_path": DEMO_MANIFEST}).json()["run_id"]
        with tc.websocket_connect(f"/api/runs/{run_id}/stream") as websocket:
            received = websocket.receive_json()

    assert received["data"] == {"result": {"findings": []}, "run_id": "run-123"}


def test_streaming_an_unknown_run_id_accepts_then_closes_with_4004():
    """The handshake must complete before the rejection.

    A pre-accept close reaches a real browser as an HTTP 403 on the handshake,
    indistinguishable from "the server is down". TestClient surfaces both
    shapes as the same WebSocketDisconnect, so this drives the ASGI app
    directly to see the accept on the wire.
    """
    sent: list[dict] = []

    async def receive():
        return {"type": "websocket.connect"}

    async def send(message):
        sent.append(message)

    scope = {
        "type": "websocket",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "scheme": "ws",
        "path": "/api/runs/nope/stream",
        "raw_path": b"/api/runs/nope/stream",
        "root_path": "",
        "query_string": b"",
        "headers": [(b"host", b"testserver")],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
        "subprotocols": [],
    }
    asyncio.run(app(scope, receive, send))

    assert [message["type"] for message in sent] == ["websocket.accept", "websocket.close"]
    assert sent[1]["code"] == WS_UNKNOWN_RUN


def test_streaming_an_unknown_run_id_reaches_the_client_as_4004():
    tc = TestClient(app)
    with pytest.raises(WebSocketDisconnect) as excinfo:
        with tc.websocket_connect("/api/runs/nope/stream") as websocket:
            websocket.receive_json()
    assert excinfo.value.code == WS_UNKNOWN_RUN
