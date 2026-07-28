"""Unit tests for the run pipeline.

Everything the pipeline touches from outside the process — Docker via the
orchestrator, HTTP via the runner, Postgres via the observer, the SQLite store,
and the Anthropic API — is patched in the ``engine.pipeline`` namespace, so no
test here needs a daemon, a database, a network, or an API key.
"""

from __future__ import annotations

from typing import Any, Iterator
from unittest.mock import MagicMock, patch

import pytest

from ai.classifier import ClassificationResult, ClassifiedFinding
from ai.intent import ChangeIntent
from ai.workflow_gen import ProposedWorkflow, WorkflowProposal
from engine import pipeline as pipeline_module
from engine.comparator import ComparisonMetadata, ComparisonResult, Finding, NoiseSummary
from engine.manifest import Manifest, parse_manifest
from engine.orchestrator import OrchestratorError, RunHandles
from engine.pipeline import RunEvent, run_pipeline
from engine.runner import ResponseCapture, RunnerError, StepResult, WorkflowResult

BASE_URL = "http://base.local"
TARGET_URL = "http://target.local"
BASE_DSN = "postgresql://u:p@localhost:5433/db"
TARGET_DSN = "postgresql://u:p@localhost:5434/db"


# -- fixtures and builders --------------------------------------------------


def _manifest_dict(*, with_database: bool = False, workflows: int = 1) -> dict[str, Any]:
    data: dict[str, Any] = {
        "app": {
            "name": "shop-api",
            "start": "uvicorn app.main:app --host 0.0.0.0 --port 8000",
            "port": 8000,
            "healthcheck": "/health",
        },
        "compare": {"base_ref": "main", "target_ref": "fix/checkout", "repo": "."},
        "workflows": [
            {"name": f"workflow-{i}", "steps": [{"method": "GET", "path": "/health"}]}
            for i in range(workflows)
        ],
    }
    if with_database:
        data["database"] = {
            "type": "postgres",
            "seed": "seed.sql",
            "observe_tables": ["carts", "orders"],
        }
    return data


def _manifest(**kwargs: Any) -> Manifest:
    return parse_manifest(_manifest_dict(**kwargs))


def _handles(*, with_dsns: bool = False) -> RunHandles:
    return RunHandles(
        base_url=BASE_URL,
        target_url=TARGET_URL,
        base_postgres_dsn=BASE_DSN if with_dsns else None,
        target_postgres_dsn=TARGET_DSN if with_dsns else None,
    )


def _workflow_result(name: str, *, steps: int = 1) -> WorkflowResult:
    response = ResponseCapture(
        status_code=200,
        headers={"content-type": "application/json"},
        body={"ok": True},
        latency_ms=1.0,
    )
    return WorkflowResult(
        name=name,
        steps=[
            StepResult(
                method="GET",
                path="/health",
                base_response=response,
                target_response=response,
            )
            for _ in range(steps)
        ],
    )


def _comparison_result(findings: int = 1) -> ComparisonResult:
    return ComparisonResult(
        findings=[
            Finding(
                category="http",
                workflow_name="workflow-0",
                step_index=0,
                summary=f"GET /health: status 200 -> 40{i}",
                evidence_base={"status_code": 200},
                evidence_target={"status_code": 400 + i},
                severity="changed",
            )
            for i in range(findings)
        ],
        noise_summary=NoiseSummary(http_suppressed=2),
        metadata=ComparisonMetadata(total_workflows=1, total_steps=1, duration_seconds=1.5),
    )


class _Patched:
    """The pipeline's collaborators, all mocked, for one test."""

    def __init__(self, orchestrator: MagicMock, run_workflows: MagicMock, compare: MagicMock, save_run: MagicMock):
        self.orchestrator_class = orchestrator
        self.orchestrator = orchestrator.return_value
        self.run_workflows = run_workflows
        self.compare = compare
        self.save_run = save_run


@pytest.fixture
def patched(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Patched]:
    """Patch the orchestrator, runner, comparator, and store with mocks.

    Defaults describe a healthy run with no database: both versions come up,
    every workflow returns one step, and the comparison finds one difference.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    orchestrator_class = MagicMock()
    orchestrator_class.return_value.start.return_value = _handles()

    run_workflows = MagicMock(
        side_effect=lambda workflows, handles, client=None: [
            _workflow_result(workflow.name) for workflow in workflows
        ]
    )
    compare = MagicMock(return_value=_comparison_result())
    save_run = MagicMock(return_value="run-123")

    with (
        patch.object(pipeline_module, "Orchestrator", orchestrator_class),
        patch.object(pipeline_module, "run_workflows", run_workflows),
        patch.object(pipeline_module, "compare", compare),
        patch.object(pipeline_module, "save_run", save_run),
    ):
        yield _Patched(orchestrator_class, run_workflows, compare, save_run)


def _stages(events: list[RunEvent]) -> list[str]:
    return [event.stage for event in events]


def _only(events: list[RunEvent], stage: str) -> RunEvent:
    matching = [event for event in events if event.stage == stage]
    assert len(matching) == 1, f"expected exactly one {stage!r} event, got {len(matching)}"
    return matching[0]


# -- stage order -------------------------------------------------------------


def test_a_successful_run_yields_its_stages_in_order(patched: _Patched) -> None:
    events = list(run_pipeline(_manifest()))

    assert _stages(events) == [
        "environments_starting",
        "environments_ready",
        "workflow_started",
        "workflow_completed",
        "workflows_complete",
        "observing_http",
        "comparing",
        "persisting",
        "done",
    ]


def test_each_workflow_is_reported_as_it_starts_and_finishes(patched: _Patched) -> None:
    events = list(run_pipeline(_manifest(workflows=3)))

    assert _stages(events).count("workflow_started") == 3
    started = [event for event in events if event.stage == "workflow_started"]
    assert [event.data["name"] for event in started] == ["workflow-0", "workflow-1", "workflow-2"]
    assert [event.data["index"] for event in started] == [0, 1, 2]
    assert {event.data["total"] for event in started} == {3}
    assert started[0].message == "Running workflow: workflow-0"

    # started/completed alternate: nothing is reported complete before it runs.
    workflow_stages = [s for s in _stages(events) if s.startswith("workflow_")]
    assert workflow_stages == [
        "workflow_started",
        "workflow_completed",
        "workflow_started",
        "workflow_completed",
        "workflow_started",
        "workflow_completed",
    ]
    assert _only(events, "workflows_complete").data == {"count": 3}


def test_every_event_carries_a_message_and_a_timestamp(patched: _Patched) -> None:
    events = list(run_pipeline(_manifest()))

    assert all(event.message for event in events)
    timestamps = [event.timestamp for event in events]
    assert timestamps == sorted(timestamps)


def test_postgres_is_observed_before_and_after_the_workflows(patched: _Patched) -> None:
    patched.orchestrator.start.return_value = _handles(with_dsns=True)
    observer_class = MagicMock()

    with patch.object(pipeline_module, "PostgresObserver", observer_class):
        events = list(run_pipeline(_manifest(with_database=True)))

    assert _stages(events) == [
        "environments_starting",
        "environments_ready",
        "observing_postgres",
        "workflow_started",
        "workflow_completed",
        "workflows_complete",
        "observing_http",
        "observing_postgres",
        "comparing",
        "persisting",
        "done",
    ]
    phases = [e.data["phase"] for e in events if e.stage == "observing_postgres"]
    assert phases == ["before", "after"]
    # One snapshot per version per phase, and the deltas are what gets compared.
    assert observer_class.return_value.snapshot.call_count == 4
    assert patched.compare.call_args.kwargs["postgres_diff"] is observer_class.compare_deltas.return_value


def test_postgres_observation_is_skipped_without_dsns(patched: _Patched) -> None:
    events = list(run_pipeline(_manifest(with_database=True)))

    skipped = _only(events, "postgres_observation_skipped")
    assert skipped.data["reason"] == "a dsn is missing for one or both versions"
    assert "observing_postgres" not in _stages(events)
    assert patched.compare.call_args.kwargs["postgres_diff"] is None


def test_a_manifest_without_a_database_reports_nothing_skipped(patched: _Patched) -> None:
    events = list(run_pipeline(_manifest()))

    assert "postgres_observation_skipped" not in _stages(events)


# -- the done event ----------------------------------------------------------


def test_done_carries_what_compare_produced(patched: _Patched) -> None:
    result = _comparison_result(findings=2)
    patched.compare.return_value = result

    done = _only(list(run_pipeline(_manifest(), manifest_path="manifest.yaml")), "done")

    assert done.data["result"] == result.model_dump(mode="json")
    assert done.data["run_id"] == "run-123"
    assert done.data["intent"] is None
    assert done.data["classification"] is None
    assert done.data["proposed_workflows"] is None
    assert done.data["models"]["result"] is result


def test_done_carries_what_the_ai_layer_produced(patched: _Patched, monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    intent = ChangeIntent(
        summary="Reject checkouts with no city",
        changed_routes=["POST /api/checkout"],
        expected_behavior_changes=["400 instead of 200 for a missing city"],
    )
    classification = ClassificationResult(
        classifications=[
            ClassifiedFinding(
                finding_index=0,
                classification="intended",
                reasoning="the diff added this validation",
                confidence=0.9,
            )
        ],
        summary="one intended change",
    )

    with (
        patch.object(pipeline_module, "extract_intent", return_value=intent) as extract,
        patch.object(pipeline_module, "classify_findings", return_value=classification) as classify,
    ):
        events = list(run_pipeline(_manifest(), diff_text="--- a/app.py", pr_description="Validate city"))

    assert "classifying" in _stages(events)
    done = _only(events, "done")
    assert done.data["intent"] == intent.model_dump(mode="json")
    assert done.data["classification"] == classification.model_dump(mode="json")
    assert done.data["models"]["intent"] is intent
    assert done.data["models"]["classification"] is classification

    extract.assert_called_once_with("--- a/app.py", "Validate city")
    assert classify.call_args.args[0] == patched.compare.return_value.findings


def test_workflow_proposals_are_only_requested_when_asked_for(patched: _Patched, monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    proposal = WorkflowProposal(
        workflows=[ProposedWorkflow(name="checkout-no-city", rationale="covers the new branch", steps=[])],
        coverage_notes="one route covered",
    )

    with (
        patch.object(pipeline_module, "extract_intent", return_value=ChangeIntent(summary="s")),
        patch.object(pipeline_module, "classify_findings", return_value=ClassificationResult(summary="s")),
        patch.object(pipeline_module, "generate_workflows", return_value=proposal) as generate,
    ):
        without = _only(list(run_pipeline(_manifest(), diff_text="d")), "done")
        with_flag = _only(
            list(run_pipeline(_manifest(), diff_text="d", generate_workflows=True)), "done"
        )

    assert without.data["proposed_workflows"] is None
    assert with_flag.data["proposed_workflows"] == proposal.model_dump(mode="json")
    assert with_flag.data["models"]["proposal"] is proposal
    generate.assert_called_once()


def test_the_ai_layer_is_skipped_without_a_diff(patched: _Patched, monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")

    with patch.object(pipeline_module, "extract_intent") as extract:
        events = list(run_pipeline(_manifest()))

    assert "classifying" not in _stages(events)
    extract.assert_not_called()


def test_the_run_is_persisted_with_the_manifest_path_and_app_name(patched: _Patched) -> None:
    result = _comparison_result()
    patched.compare.return_value = result

    list(run_pipeline(_manifest(), manifest_path="demo/manifests/scenario1.yaml"))

    patched.save_run.assert_called_once_with(
        manifest_path="demo/manifests/scenario1.yaml",
        app_name="shop-api",
        result=result.model_dump(mode="json"),
        intent=None,
        classification=None,
    )


def test_json_data_drops_the_in_process_only_keys(patched: _Patched) -> None:
    done = _only(list(run_pipeline(_manifest())), "done")

    assert "models" in done.data
    assert "models" not in done.json_data()
    assert set(done.json_data()) == {"result", "intent", "classification", "proposed_workflows", "run_id"}


def test_run_event_rejects_unknown_fields() -> None:
    with pytest.raises(Exception):
        RunEvent(stage="done", message="m", timestamp=1.0, data=None, extra="nope")


# -- failures ----------------------------------------------------------------


def test_an_orchestrator_error_ends_the_run_cleanly(patched: _Patched) -> None:
    patched.orchestrator.start.side_effect = OrchestratorError("seed file not found: seed.sql")

    events = list(run_pipeline(_manifest()))

    assert _stages(events) == ["environments_starting", "error"]
    error = events[-1]
    assert error.data["error"] == "seed file not found: seed.sql"
    assert error.data["error_type"] == "OrchestratorError"
    assert error.data["expected"] is True
    assert isinstance(error.data["exception"], OrchestratorError)
    # The run stopped there: nothing was compared or saved.
    patched.compare.assert_not_called()
    patched.save_run.assert_not_called()
    patched.orchestrator.cleanup.assert_called_once_with()


def test_a_runner_error_mid_run_still_tears_the_environment_down(patched: _Patched) -> None:
    patched.run_workflows.side_effect = RunnerError("request failed: GET /health")

    events = list(run_pipeline(_manifest()))

    assert _stages(events) == [
        "environments_starting",
        "environments_ready",
        "workflow_started",
        "error",
    ]
    assert events[-1].data["expected"] is True
    patched.orchestrator.cleanup.assert_called_once_with()


def test_an_unexpected_error_is_reported_as_unexpected(patched: _Patched) -> None:
    patched.compare.side_effect = ValueError("bug in the comparator")

    events = list(run_pipeline(_manifest()))

    error = events[-1]
    assert error.stage == "error"
    assert error.data["error_type"] == "ValueError"
    assert error.data["expected"] is False
    assert isinstance(error.data["exception"], ValueError)
    patched.orchestrator.cleanup.assert_called_once_with()


def test_abandoning_the_generator_still_tears_the_environment_down(patched: _Patched) -> None:
    events = run_pipeline(_manifest())
    next(events)  # environments_starting
    next(events)  # environments_ready, so the orchestrator has started
    events.close()

    patched.orchestrator.cleanup.assert_called_once_with()


# -- direct mode -------------------------------------------------------------


def test_direct_mode_skips_orchestration_entirely(patched: _Patched) -> None:
    events = list(
        run_pipeline(
            _manifest(),
            base_url="http://localhost:8001/",
            target_url="http://localhost:8002/",
        )
    )

    patched.orchestrator_class.assert_not_called()
    assert _stages(events)[0] == "environments_starting"
    assert events[0].data["direct"] is True
    ready = _only(events, "environments_ready")
    # Trailing slashes are stripped so paths append cleanly.
    assert ready.data == {"base_url": "http://localhost:8001", "target_url": "http://localhost:8002"}
    assert _stages(events)[-1] == "done"


def test_direct_mode_passes_the_given_dsns_through(patched: _Patched) -> None:
    observer_class = MagicMock()

    with patch.object(pipeline_module, "PostgresObserver", observer_class):
        events = list(
            run_pipeline(
                _manifest(with_database=True),
                base_url=BASE_URL,
                target_url=TARGET_URL,
                base_pg_dsn=BASE_DSN,
                target_pg_dsn=TARGET_DSN,
            )
        )

    assert "postgres_observation_skipped" not in _stages(events)
    assert [call.args[0] for call in observer_class.call_args_list] == [
        BASE_DSN,
        TARGET_DSN,
        BASE_DSN,
        TARGET_DSN,
    ]


def test_direct_mode_without_dsns_skips_database_observation(patched: _Patched) -> None:
    events = list(
        run_pipeline(_manifest(with_database=True), base_url=BASE_URL, target_url=TARGET_URL)
    )

    assert "postgres_observation_skipped" in _stages(events)
    assert _stages(events)[-1] == "done"


def test_direct_mode_runs_workflows_against_the_given_urls(patched: _Patched) -> None:
    list(run_pipeline(_manifest(), base_url=BASE_URL, target_url=TARGET_URL))

    handles = patched.run_workflows.call_args.args[1]
    assert handles.base_url == BASE_URL
    assert handles.target_url == TARGET_URL


def test_one_http_client_is_shared_across_workflows(patched: _Patched) -> None:
    list(run_pipeline(_manifest(workflows=3)))

    clients = {id(call.kwargs["client"]) for call in patched.run_workflows.call_args_list}
    assert len(clients) == 1
