"""Unit tests for the live-run registry.

``run_pipeline`` and ``load_manifest`` are patched in the
``web.run_registry`` namespace, so nothing here starts a container, touches a
database, or reads a manifest off disk — the tests are about threading,
queueing, and status transitions only.
"""

from __future__ import annotations

import queue
import threading
import time
from typing import Iterator
from unittest.mock import MagicMock, patch

import pytest

from engine.manifest import ManifestError
from engine.pipeline import RunEvent
from web import run_registry

MANIFEST_PATH = "demo/manifests/scenario1-checkout-validation.yaml"


# -- helpers -----------------------------------------------------------------


def _event(stage: str, message: str = "", data: dict | None = None) -> RunEvent:
    return RunEvent(stage=stage, message=message or stage, timestamp=time.time(), data=data)


def _canned(*events: RunEvent):
    """A run_pipeline stand-in that yields the given events."""

    def fake_run_pipeline(manifest, **kwargs):
        yield from events

    return MagicMock(side_effect=fake_run_pipeline)


def _drain(run_id: str, timeout: float = 5.0) -> list[RunEvent]:
    """Read the run's queue up to and including the sentinel."""
    events_queue = run_registry.get_queue(run_id)
    assert events_queue is not None
    collected: list[RunEvent] = []
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        assert remaining > 0, "timed out waiting for the sentinel"
        item = events_queue.get(timeout=remaining)
        if item is None:
            return collected
        collected.append(item)


def _await_finish(run_id: str, timeout: float = 5.0) -> str:
    """Drain the run and return its final status."""
    _drain(run_id, timeout=timeout)
    entry = run_registry.get_run(run_id)
    assert entry is not None and entry.thread is not None
    entry.thread.join(timeout=timeout)
    status = run_registry.get_status(run_id)
    assert status is not None
    return status


@pytest.fixture(autouse=True)
def clean_registry() -> Iterator[None]:
    run_registry.clear()
    yield
    run_registry.clear()


@pytest.fixture
def loaded_manifest() -> Iterator[MagicMock]:
    """Patch out manifest loading; the registry's job isn't parsing YAML."""
    load_manifest = MagicMock(return_value=MagicMock(name="manifest"))
    with patch.object(run_registry, "load_manifest", load_manifest):
        yield load_manifest


# -- start_run does not block ------------------------------------------------


def test_start_run_returns_a_run_id_without_waiting_for_the_pipeline(loaded_manifest) -> None:
    release = threading.Event()

    def slow_pipeline(manifest, **kwargs):
        release.wait(timeout=5)
        yield _event("done")

    with patch.object(run_registry, "run_pipeline", MagicMock(side_effect=slow_pipeline)):
        started = time.monotonic()
        run_id = run_registry.start_run(MANIFEST_PATH)
        elapsed = time.monotonic() - started

        try:
            assert isinstance(run_id, str) and run_id
            assert elapsed < 1.0, f"start_run blocked for {elapsed:.2f}s"
            assert run_registry.get_status(run_id) == "running"
        finally:
            release.set()

        assert _await_finish(run_id) == "done"


def test_start_run_spawns_a_live_background_thread(loaded_manifest) -> None:
    with patch.object(run_registry, "run_pipeline", _canned(_event("done"))):
        run_id = run_registry.start_run(MANIFEST_PATH)

        entry = run_registry.get_run(run_id)
        assert entry is not None
        assert isinstance(entry.thread, threading.Thread)
        assert entry.thread is not threading.current_thread()
        assert entry.thread.daemon

        _await_finish(run_id)


def test_each_run_gets_its_own_id_and_queue(loaded_manifest) -> None:
    with patch.object(run_registry, "run_pipeline", _canned(_event("done"))):
        first = run_registry.start_run(MANIFEST_PATH)
        second = run_registry.start_run(MANIFEST_PATH)

    assert first != second
    assert run_registry.get_queue(first) is not run_registry.get_queue(second)
    _await_finish(first)
    _await_finish(second)


def test_the_pipeline_is_given_the_manifest_and_its_path(loaded_manifest) -> None:
    run_pipeline = _canned(_event("done"))
    with patch.object(run_registry, "run_pipeline", run_pipeline):
        run_id = run_registry.start_run(MANIFEST_PATH, diff_text="a diff")
        _await_finish(run_id)

    loaded_manifest.assert_called_once_with(MANIFEST_PATH)
    args, kwargs = run_pipeline.call_args
    assert args == (loaded_manifest.return_value,)
    assert kwargs == {"manifest_path": MANIFEST_PATH, "diff_text": "a diff"}


# -- events reach the queue --------------------------------------------------


def test_events_arrive_in_order_and_end_with_a_sentinel(loaded_manifest) -> None:
    events = (
        _event("environments_starting"),
        _event("environments_ready"),
        _event("workflow_started", data={"name": "checkout"}),
        _event("done", data={"run_id": "run-123"}),
    )
    with patch.object(run_registry, "run_pipeline", _canned(*events)):
        run_id = run_registry.start_run(MANIFEST_PATH)
        streamed = _drain(run_id)

    assert [event.stage for event in streamed] == [
        "environments_starting",
        "environments_ready",
        "workflow_started",
        "done",
    ]
    assert streamed[-1].data == {"run_id": "run-123"}

    # The sentinel _drain consumed was the last thing on the queue.
    events_queue = run_registry.get_queue(run_id)
    assert events_queue is not None
    with pytest.raises(queue.Empty):
        events_queue.get(timeout=0.2)


def test_an_unknown_run_id_has_no_queue_and_no_status() -> None:
    assert run_registry.get_queue("nope") is None
    assert run_registry.get_status("nope") is None
    assert run_registry.get_run("nope") is None


# -- status transitions ------------------------------------------------------


def test_status_goes_from_running_to_done(loaded_manifest) -> None:
    gate = threading.Event()

    def gated_pipeline(manifest, **kwargs):
        yield _event("environments_ready")
        gate.wait(timeout=5)
        yield _event("done", data={"run_id": "run-123"})

    with patch.object(run_registry, "run_pipeline", MagicMock(side_effect=gated_pipeline)):
        run_id = run_registry.start_run(MANIFEST_PATH)
        events_queue = run_registry.get_queue(run_id)
        assert events_queue is not None

        assert events_queue.get(timeout=5).stage == "environments_ready"
        assert run_registry.get_status(run_id) == "running"

        gate.set()
        assert _await_finish(run_id) == "done"


def test_status_becomes_error_when_the_run_ends_in_an_error(loaded_manifest) -> None:
    events = (
        _event("environments_starting"),
        _event("error", "boom", data={"error": "boom", "expected": True}),
    )
    with patch.object(run_registry, "run_pipeline", _canned(*events)):
        run_id = run_registry.start_run(MANIFEST_PATH)
        streamed = _drain(run_id)

    assert [event.stage for event in streamed] == ["environments_starting", "error"]
    assert run_registry.get_status(run_id) == "error"


def test_the_status_is_final_by_the_time_the_sentinel_arrives(loaded_manifest) -> None:
    with patch.object(run_registry, "run_pipeline", _canned(_event("done"))):
        run_id = run_registry.start_run(MANIFEST_PATH)
        events_queue = run_registry.get_queue(run_id)
        assert events_queue is not None

        while events_queue.get(timeout=5) is not None:
            pass
        # No join(): reading the sentinel is enough to trust the status.
        assert run_registry.get_status(run_id) == "done"


def test_a_bad_manifest_becomes_an_error_event_rather_than_raising() -> None:
    load_manifest = MagicMock(side_effect=ManifestError("manifest file not found: gone.yaml"))
    with patch.object(run_registry, "load_manifest", load_manifest):
        run_id = run_registry.start_run("gone.yaml")
        streamed = _drain(run_id)

    assert [event.stage for event in streamed] == ["error"]
    assert "gone.yaml" in streamed[0].message
    assert run_registry.get_status(run_id) == "error"


def test_an_unexpected_failure_still_ends_the_stream(loaded_manifest) -> None:
    def exploding_pipeline(manifest, **kwargs):
        yield _event("environments_starting")
        raise RuntimeError("kaboom")

    with patch.object(run_registry, "run_pipeline", MagicMock(side_effect=exploding_pipeline)):
        run_id = run_registry.start_run(MANIFEST_PATH)
        streamed = _drain(run_id)

    assert [event.stage for event in streamed] == ["environments_starting", "error"]
    assert streamed[-1].data["error_type"] == "RuntimeError"
    assert streamed[-1].data["expected"] is False
    assert run_registry.get_status(run_id) == "error"


def test_a_pipeline_that_yields_no_terminal_event_is_reported_as_an_error(loaded_manifest) -> None:
    with patch.object(run_registry, "run_pipeline", _canned(_event("environments_ready"))):
        run_id = run_registry.start_run(MANIFEST_PATH)
        streamed = _drain(run_id)

    assert [event.stage for event in streamed] == ["environments_ready", "error"]
    assert run_registry.get_status(run_id) == "error"


# -- eviction ----------------------------------------------------------------


def test_finished_runs_are_evicted_oldest_first_past_the_cap(loaded_manifest) -> None:
    with patch.object(run_registry, "run_pipeline", _canned(_event("done"))):
        run_ids = []
        for _ in range(run_registry.MAX_RUNS + 5):
            run_id = run_registry.start_run(MANIFEST_PATH)
            _await_finish(run_id)
            run_ids.append(run_id)

    assert len(run_registry.list_active_runs()) <= run_registry.MAX_RUNS
    assert run_registry.get_status(run_ids[0]) is None, "the oldest run should have been evicted"
    assert run_registry.get_status(run_ids[-1]) == "done", "the newest run must be kept"


def test_a_run_still_in_flight_is_never_evicted(loaded_manifest) -> None:
    release = threading.Event()

    def slow_pipeline(manifest, **kwargs):
        release.wait(timeout=10)
        yield _event("done")

    with patch.object(run_registry, "run_pipeline", MagicMock(side_effect=slow_pipeline)):
        in_flight = run_registry.start_run(MANIFEST_PATH)

    try:
        with patch.object(run_registry, "run_pipeline", _canned(_event("done"))):
            for _ in range(run_registry.MAX_RUNS + 5):
                _await_finish(run_registry.start_run(MANIFEST_PATH))

        assert run_registry.get_status(in_flight) == "running"
    finally:
        release.set()

    _await_finish(in_flight)
