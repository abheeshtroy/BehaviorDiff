"""In-memory registry of live runs, so the dashboard can watch one happen.

A run is ``run_pipeline`` executing on a background thread, with every event it
yields pushed onto that run's queue. The queue is the handoff point: the
pipeline is synchronous and blocking, the web layer is async, and a
``queue.Queue`` is the thread-safe boundary between them. A consumer reads the
queue until it gets the ``None`` sentinel, which is pushed exactly once, after
the run's status has been set to its final value.

The registry is process-local and deliberately small — it holds live progress,
not history. Finished runs are persisted by the pipeline itself (``web.store``)
and read back through ``/api/runs``; entries here are evicted once there are
more than ``MAX_RUNS`` of them.
"""

from __future__ import annotations

import queue
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import structlog

from engine.manifest import ManifestError, load_manifest
from engine.pipeline import RunEvent, run_pipeline

log = structlog.get_logger(__name__)

RunStatus = Literal["running", "done", "error"]

# How many run entries to keep. Older finished runs are evicted past this;
# a run still in flight is never evicted out from under its consumer.
MAX_RUNS = 50


@dataclass
class RunEntry:
    """One run: its status, the events it has produced, and the thread producing them."""

    run_id: str
    manifest_path: str
    status: RunStatus = "running"
    events: queue.Queue = field(default_factory=queue.Queue)
    thread: threading.Thread | None = None
    started_at: float = field(default_factory=time.time)


_runs: OrderedDict[str, RunEntry] = OrderedDict()
_lock = threading.Lock()


def start_run(manifest_path: str | Path, **pipeline_kwargs: Any) -> str:
    """Start a run in the background and return its id immediately.

    The manifest is loaded on the run's own thread rather than here, so a bad
    manifest surfaces as an ``error`` event on the stream like any other
    failure instead of raising out of the caller's request handler.
    """
    run_id = uuid.uuid4().hex
    entry = RunEntry(run_id=run_id, manifest_path=str(manifest_path))

    thread = threading.Thread(
        target=_drive_run,
        args=(entry, str(manifest_path), pipeline_kwargs),
        name=f"behaviordiff-run-{run_id[:8]}",
        daemon=True,
    )
    entry.thread = thread

    with _lock:
        _runs[run_id] = entry
        _evict_locked()

    # Registered before started, so get_queue() can never miss an event.
    thread.start()
    log.info("run_started", run_id=run_id, manifest_path=str(manifest_path))
    return run_id


def get_queue(run_id: str) -> queue.Queue | None:
    """The event queue for a run, or None if no such run is registered."""
    with _lock:
        entry = _runs.get(run_id)
    return entry.events if entry else None


def get_status(run_id: str) -> RunStatus | None:
    """The run's status, or None if no such run is registered."""
    with _lock:
        entry = _runs.get(run_id)
    return entry.status if entry else None


def get_run(run_id: str) -> RunEntry | None:
    """The whole entry, for callers that want more than one field of it."""
    with _lock:
        return _runs.get(run_id)


def list_active_runs() -> list[RunEntry]:
    """Every registered run, oldest first."""
    with _lock:
        return list(_runs.values())


def clear() -> None:
    """Drop every entry. For tests — running threads are left to finish."""
    with _lock:
        _runs.clear()


def _drive_run(entry: RunEntry, manifest_path: str, pipeline_kwargs: dict[str, Any]) -> None:
    """Body of a run's thread: pump pipeline events onto the run's queue.

    Ends by setting the final status and pushing the sentinel, whatever
    happened — a consumer blocked on the queue must always be released.
    """
    terminal: str | None = None
    try:
        manifest = load_manifest(manifest_path)
        for event in run_pipeline(manifest, manifest_path=manifest_path, **pipeline_kwargs):
            if event.stage in ("done", "error"):
                terminal = event.stage
                # Status first, sentinel last: a consumer that sees the
                # sentinel can read the final status without racing.
                _set_status(entry, "done" if event.stage == "done" else "error")
            entry.events.put(event)
    except ManifestError as exc:
        terminal = "error"
        _set_status(entry, "error")
        entry.events.put(_failure_event(exc, expected=True))
    except Exception as exc:  # noqa: BLE001 - reported on the stream, not swallowed
        log.exception("run_thread_failed", run_id=entry.run_id)
        terminal = "error"
        _set_status(entry, "error")
        entry.events.put(_failure_event(exc, expected=False))
    finally:
        if terminal is None:
            # The pipeline promises a terminal event; not getting one is a bug
            # in it, and the consumer deserves to hear about it either way.
            _set_status(entry, "error")
            entry.events.put(
                RunEvent(
                    stage="error",
                    message="the run ended without a terminal event",
                    timestamp=time.time(),
                    data={"error": "the run ended without a terminal event", "expected": False},
                )
            )
        entry.events.put(None)
        log.info("run_finished", run_id=entry.run_id, status=entry.status)


def _failure_event(exc: Exception, *, expected: bool) -> RunEvent:
    return RunEvent(
        stage="error",
        message=str(exc),
        timestamp=time.time(),
        data={
            "error": str(exc),
            "error_type": type(exc).__name__,
            "expected": expected,
            "exception": exc,
        },
    )


def _set_status(entry: RunEntry, status: RunStatus) -> None:
    with _lock:
        entry.status = status


def _evict_locked() -> None:
    """Trim the registry to MAX_RUNS, dropping finished runs oldest-first.

    Caller holds _lock. A run still in flight is skipped: evicting it would
    strand whoever is reading its queue.
    """
    if len(_runs) <= MAX_RUNS:
        return
    for run_id, entry in list(_runs.items()):
        if len(_runs) <= MAX_RUNS:
            return
        if entry.status != "running":
            del _runs[run_id]
