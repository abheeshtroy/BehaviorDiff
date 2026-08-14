"""Unit tests for the `--init` CLI path.

Every test patches anthropic.Anthropic and sets ANTHROPIC_API_KEY on a
monkeypatched environment, so nothing here reaches the network. The manifest is
always written under tmp_path — no test touches the working directory.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import anthropic
import httpx
import pytest

import cli
from engine.comparator import ComparisonResult

DOCKERFILE = """\
FROM python:3.12-slim
EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0"]
"""

APP_PY = '''\
@app.get("/health")
async def health(): ...


@app.post("/api/carts")
async def create_cart(payload: dict):
    await db.execute("INSERT INTO carts (id) VALUES ($1)", payload["id"])
'''

GENERATED = """\
app:
  name: shop-api
  start: uvicorn app:app --host 0.0.0.0 --port 8000
  port: 8000
  healthcheck: /health

compare:
  base_ref: main
  target_ref: HEAD
  repo: .

workflows:
  - name: create-cart
    steps:
      - method: POST
        path: /api/carts
        body: {"id": "abc"}
"""


class _StubClient:
    """Stands in for anthropic.Anthropic(); replays a canned reply or raises."""

    def __init__(self, *, text: str | None = None, error: Exception | None = None):
        self._text = text
        self._error = error
        self.calls: list[dict] = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=self._text)])


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "Dockerfile").write_text(DOCKERFILE)
    (tmp_path / "app.py").write_text(APP_PY)
    return tmp_path


@pytest.fixture
def with_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")


def _run(monkeypatch: pytest.MonkeyPatch, *argv: str) -> int:
    monkeypatch.setattr("sys.argv", ["behaviordiff", *argv])
    return cli.main()


def _patch_client(stub: _StubClient):
    return patch("anthropic.Anthropic", return_value=stub)


def test_init_writes_behaviordiff_yaml(repo: Path, monkeypatch, with_api_key, capsys):
    stub = _StubClient(text=GENERATED)
    with _patch_client(stub):
        code = _run(monkeypatch, "--init", str(repo))

    assert code == 0
    written = repo / "behaviordiff.yaml"
    assert written.read_text().strip() == GENERATED.strip()
    assert str(written) in capsys.readouterr().out


def test_init_defaults_to_the_current_directory(repo: Path, monkeypatch, with_api_key):
    monkeypatch.chdir(repo)
    stub = _StubClient(text=GENERATED)
    with _patch_client(stub):
        code = _run(monkeypatch, "--init")

    assert code == 0
    assert (repo / "behaviordiff.yaml").exists()
    content = stub.calls[0]["messages"][0]["content"]
    assert "main" in content
    assert "HEAD" in content


def test_init_writes_generated_yaml_when_the_manifest_exists(
    repo: Path, monkeypatch, with_api_key, capsys
):
    existing = repo / "behaviordiff.yaml"
    existing.write_text("# hand-written, do not clobber\n")

    stub = _StubClient(text=GENERATED)
    with _patch_client(stub):
        code = _run(monkeypatch, "--init", str(repo))

    assert code == 0
    # The existing manifest is left exactly as it was.
    assert existing.read_text() == "# hand-written, do not clobber\n"
    assert (repo / "behaviordiff.generated.yaml").read_text().strip() == GENERATED.strip()
    assert "already exists" in capsys.readouterr().out


def test_init_without_api_key_exits_with_an_error(repo: Path, monkeypatch, capsys):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    stub = _StubClient(text=GENERATED)
    with _patch_client(stub):
        code = _run(monkeypatch, "--init", str(repo))

    assert code == 1
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err
    assert not (repo / "behaviordiff.yaml").exists()
    # It fails before spending a call, not after.
    assert stub.calls == []


def test_init_on_a_missing_directory_exits_with_an_error(
    tmp_path: Path, monkeypatch, with_api_key, capsys
):
    stub = _StubClient(text=GENERATED)
    with _patch_client(stub):
        code = _run(monkeypatch, "--init", str(tmp_path / "nope"))

    assert code == 1
    assert "not a directory" in capsys.readouterr().err
    assert stub.calls == []


def test_init_reports_an_api_failure(repo: Path, monkeypatch, with_api_key, capsys):
    stub = _StubClient(
        error=anthropic.APIConnectionError(
            request=httpx.Request("POST", "https://api.anthropic.com")
        )
    )
    with _patch_client(stub):
        code = _run(monkeypatch, "--init", str(repo))

    assert code == 2
    assert "manifest generation failed" in capsys.readouterr().err
    assert not (repo / "behaviordiff.yaml").exists()


def test_init_prints_the_scan_before_generating(repo: Path, monkeypatch, with_api_key, capsys):
    stub = _StubClient(text=GENERATED)
    with _patch_client(stub):
        _run(monkeypatch, "--init", str(repo))

    out = capsys.readouterr().out
    assert "GET /health" in out
    assert "POST /api/carts" in out
    assert "carts" in out
    assert "8000" in out
    assert "uvicorn app:app" in out


def test_init_passes_refs_and_pr_description_through(repo: Path, monkeypatch, with_api_key):
    stub = _StubClient(text=GENERATED)
    with _patch_client(stub):
        code = _run(
            monkeypatch,
            "--init",
            str(repo),
            "--base-ref",
            "release/2.0",
            "--target-ref",
            "fix/checkout",
            "--pr-description",
            "Reject checkouts with no city.",
        )

    assert code == 0
    content = stub.calls[0]["messages"][0]["content"]
    assert "release/2.0" in content
    assert "fix/checkout" in content
    assert "Reject checkouts with no city." in content


def test_running_without_a_manifest_or_init_is_a_usage_error(monkeypatch, capsys):
    with pytest.raises(SystemExit) as exc:
        _run(monkeypatch)

    assert exc.value.code == 2
    assert "manifest path is required" in capsys.readouterr().err


def test_invalid_manifest_exits_with_setup_failure(monkeypatch, tmp_path: Path, capsys):
    manifest_path = tmp_path / "behaviordiff.yaml"
    manifest_path.write_text("app: not-a-valid-manifest\n")

    assert _run(monkeypatch, str(manifest_path)) == 2
    captured = capsys.readouterr()
    assert "Manifest error:" in captured.err


def test_json_invalid_manifest_exits_two_with_diagnostics_only_on_stderr(
    monkeypatch, tmp_path: Path, capsys
):
    manifest_path = tmp_path / "behaviordiff.yaml"
    manifest_path.write_text("app: not-a-valid-manifest\n")

    assert _run(monkeypatch, str(manifest_path), "--json") == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Manifest error:" in captured.err


def test_runtime_refs_override_manifest_in_memory_without_rewriting(
    tmp_path: Path, monkeypatch
):
    manifest_path = tmp_path / "behaviordiff.yaml"
    original = """\
app:
  name: sample
  start: python -m http.server 8000
  port: 8000
  healthcheck: /
compare:
  base_ref: main
  target_ref: HEAD
workflows:
  - name: home
    steps:
      - method: GET
        path: /
"""
    manifest_path.write_text(original)
    seen = {}

    def fake_pipeline(manifest, **kwargs):
        seen["manifest"] = manifest
        yield cli.RunEvent(stage="done", message="done", timestamp=0, data={"run_id": "test"})

    monkeypatch.setattr(cli, "run_pipeline", fake_pipeline)
    monkeypatch.setattr(cli, "_report_result", lambda event, args: 0)

    assert _run(
        monkeypatch,
        str(manifest_path),
        "--base-ref",
        "refs/pull/42/base",
        "--target-ref",
        "refs/pull/42/head",
    ) == 0
    assert seen["manifest"].compare.base_ref == "refs/pull/42/base"
    assert seen["manifest"].compare.target_ref == "refs/pull/42/head"
    assert manifest_path.read_text() == original


def test_json_output_shape_matches_action_summary_fields(capsys):
    result = ComparisonResult(
        metadata={"total_workflows": 2, "total_steps": 3, "duration_seconds": 0.1}
    )
    event = cli.RunEvent(
        stage="done",
        message="done",
        timestamp=0,
        data={
            "run_id": "test1234",
            "result": result.model_dump(mode="json"),
            "models": {
                "result": result,
                "intent": None,
                "classification": None,
                "proposal": None,
            },
        },
    )
    args = SimpleNamespace(json_output=True, verbose=False)

    assert cli._report_result(event, args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["findings"] == []
    assert payload["metadata"]["total_workflows"] == 2


def _completed_event(result: ComparisonResult) -> cli.RunEvent:
    return cli.RunEvent(
        stage="done",
        message="done",
        timestamp=0,
        data={
            "run_id": "test1234",
            "result": result.model_dump(mode="json"),
            "models": {
                "result": result,
                "intent": None,
                "classification": None,
                "proposal": None,
            },
        },
    )


def _stub_completed_run(monkeypatch, result: ComparisonResult) -> None:
    monkeypatch.setattr(
        cli,
        "load_manifest",
        lambda path: SimpleNamespace(
            app=SimpleNamespace(name="sample"),
            workflows=[],
            compare=SimpleNamespace(base_ref="main", target_ref="HEAD"),
        ),
    )
    monkeypatch.setattr(cli, "run_pipeline", lambda *args, **kwargs: iter([_completed_event(result)]))


def test_main_json_stdout_is_a_single_document_and_logs_go_to_stderr(
    monkeypatch, capsys
):
    result = ComparisonResult(
        metadata={"total_workflows": 0, "total_steps": 0, "duration_seconds": 0}
    )
    _stub_completed_run(monkeypatch, result)
    monkeypatch.setattr("sys.argv", ["behaviordiff", "manifest.yaml", "--json"])

    assert cli.main() == 0

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["findings"] == []
    assert "manifest_loaded" not in captured.out
    assert "manifest_loaded" in captured.err


def test_main_json_findings_keep_exit_code_one(monkeypatch, capsys):
    result = ComparisonResult(
        findings=[
            {
                "category": "http",
                "workflow_name": "checkout",
                "step_index": 0,
                "summary": "status 200 -> 500",
                "severity": "changed",
            }
        ],
        metadata={"total_workflows": 1, "total_steps": 1, "duration_seconds": 0},
    )
    _stub_completed_run(monkeypatch, result)
    monkeypatch.setattr("sys.argv", ["behaviordiff", "manifest.yaml", "--json"])

    assert cli.main() == 1
    payload = json.loads(capsys.readouterr().out)
    assert len(payload["findings"]) == 1
