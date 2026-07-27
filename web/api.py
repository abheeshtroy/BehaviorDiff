"""FastAPI backend for BehaviorDiff dashboard."""

from __future__ import annotations

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from web.store import list_runs, get_run

app = FastAPI(title="BehaviorDiff", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


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
