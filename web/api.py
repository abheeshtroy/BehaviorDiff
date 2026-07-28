"""FastAPI backend for BehaviorDiff dashboard."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from web.store import list_runs, get_run

app = FastAPI(title="BehaviorDiff", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)

DIST_DIR = Path(__file__).parent / "frontend" / "dist"


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
