"""FastAPI backend for BehaviorDiff dashboard."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import structlog
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from engine.manifest import ManifestError, load_manifest
from engine.pipeline import RunEvent
from web import run_registry
from web.store import list_runs, get_run

log = structlog.get_logger(__name__)

app = FastAPI(title="BehaviorDiff", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)

DIST_DIR = Path(__file__).parent / "frontend" / "dist"

# Where triggerable manifests live. This directory is also the security
# boundary for POST /api/runs/trigger: a run can only be started from a
# manifest discovered here, never from an arbitrary path in the request.
MANIFEST_DIR_ENV = "BEHAVIORDIFF_MANIFEST_DIR"
DEFAULT_MANIFEST_DIR = "demo/manifests"

# Close codes for the run stream (4000-4999 is the application-defined range).
WS_UNKNOWN_RUN = 4004


def manifest_dir() -> Path:
    """The directory scanned for manifests, overridable by env var."""
    return Path(os.environ.get(MANIFEST_DIR_ENV) or DEFAULT_MANIFEST_DIR)


def discover_manifests() -> list[dict[str, Any]]:
    """Every *.yaml in the manifest directory, with metadata where it parses.

    A manifest that fails to validate is still listed — the dashboard should
    show it and say why it's broken — but with null metadata. One bad file
    must not blank the whole list.
    """
    directory = manifest_dir()
    if not directory.is_dir():
        log.warning("manifest_dir_missing", path=str(directory))
        return []

    manifests: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.yaml")):
        entry: dict[str, Any] = {
            "path": str(path),
            "filename": path.name,
            "app_name": None,
            "workflow_count": None,
            "error": None,
        }
        try:
            manifest = load_manifest(path)
        except ManifestError as exc:
            entry["error"] = str(exc)
            log.warning("manifest_unparseable", path=str(path), error=str(exc))
        else:
            entry["app_name"] = manifest.app.name
            entry["workflow_count"] = len(manifest.workflows)
        manifests.append(entry)
    return manifests


class TriggerRequest(BaseModel):
    manifest_path: str


@app.get("/api/manifests")
def api_list_manifests():
    # bench*.yaml are internal evaluation cases driven by bench/eval.py, not
    # user-facing demos — keep them out of the dashboard's picker.
    return [entry for entry in discover_manifests() if not entry["filename"].startswith("bench")]


@app.post("/api/runs/trigger")
def api_trigger_run(request: TriggerRequest):
    """Start a run from one of the discovered manifests.

    The requested path is matched by resolved path against the discovered set,
    so neither traversal ("../../etc/passwd") nor a symlink dressed up as a
    manifest gets past it.
    """
    allowed = {str(Path(entry["path"]).resolve()): entry["path"] for entry in discover_manifests()}
    requested = str(Path(request.manifest_path).resolve())

    if requested not in allowed:
        log.warning("run_trigger_rejected", manifest_path=request.manifest_path)
        raise HTTPException(
            status_code=403,
            detail=f"manifest_path is not one of the manifests in {manifest_dir()}",
        )

    run_id = run_registry.start_run(allowed[requested])
    return {"run_id": run_id}


@app.websocket("/api/runs/{run_id}/stream")
async def api_stream_run(websocket: WebSocket, run_id: str):
    """Stream a live run's events to the browser until it ends.

    The pipeline runs on a thread and hands events over a queue.Queue, so the
    blocking get() goes to an executor to keep this event loop free.

    Unknown run ids are rejected *after* the handshake completes: accept, then
    close with WS_UNKNOWN_RUN. Closing before accept would surface to a real
    client as an opaque HTTP 403 on the handshake, which the browser cannot
    tell apart from "the server is down"; a close code it can read.
    """
    await websocket.accept()

    events = run_registry.get_queue(run_id)
    if events is None:
        log.warning("run_stream_unknown_run", run_id=run_id)
        await websocket.close(code=WS_UNKNOWN_RUN, reason=f"unknown run id: {run_id}")
        return

    loop = asyncio.get_running_loop()
    try:
        while True:
            event = await loop.run_in_executor(None, events.get)
            if event is None:  # sentinel: the run is over
                break
            await websocket.send_json(_serialize_event(event))
    except WebSocketDisconnect:
        log.info("run_stream_disconnected", run_id=run_id)
        return

    await websocket.close()


def _serialize_event(event: RunEvent) -> dict[str, Any]:
    """A RunEvent as wire-safe JSON.

    json_data() is what drops the in-process-only payload (live pydantic
    models, the raw exception) that can't cross a socket.
    """
    return {
        "stage": event.stage,
        "message": event.message,
        "timestamp": event.timestamp,
        "data": event.json_data(),
    }


@app.get("/api/runs")
def api_list_runs(limit: int = 50):
    return list_runs(limit=limit)


@app.get("/api/runs/{run_id}")
def api_get_run(run_id: str):
    run = get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return run


@app.get("/api/health")
def health():
    return {"status": "ok"}


# Serve React static files if the build exists
if DIST_DIR.is_dir():
    app.mount("/assets", StaticFiles(directory=DIST_DIR / "assets"), name="assets")

    @app.get("/{full_path:path}")
    def serve_spa(full_path: str):
        """Serve the React SPA for any non-API route."""
        file_path = DIST_DIR / full_path
        if full_path and file_path.is_file():
            return FileResponse(file_path)
        return FileResponse(DIST_DIR / "index.html")
