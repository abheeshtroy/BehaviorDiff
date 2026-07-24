"""Unit tests for the `--init` CLI path.

Every test patches anthropic.Anthropic and sets ANTHROPIC_API_KEY on a
monkeypatched environment, so nothing here reaches the network. The manifest is
always written under tmp_path — no test touches the working directory.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import anthropic
import httpx
import pytest

import cli

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
