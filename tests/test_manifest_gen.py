"""Unit tests for repo scanning and manifest generation.

scan_repo is pure text heuristics against a temp directory — no API, no network.
Every generate_manifest test replaces anthropic.Anthropic with a stub, so nothing
here reaches the network or needs an API key.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import anthropic
import httpx
import pytest
from structlog.testing import capture_logs

from ai.manifest_gen import (
    ManifestGenerationError,
    RepoContext,
    generate_manifest,
    scan_repo,
)

DOCKERFILE = """\
FROM python:3.12-slim
WORKDIR /app
COPY . .
RUN pip install -r requirements.txt
EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0"]
"""

APP_PY = '''\
from fastapi import FastAPI

app = FastAPI()


@app.get("/health")
async def health():
    return {"ok": True}


@app.post("/api/carts")
async def create_cart(payload: dict):
    await db.execute("INSERT INTO carts (id) VALUES ($1)", payload["id"])
    return {"cart_id": payload["id"]}


@app.post("/api/checkout")
async def checkout(payload: dict):
    await db.execute("INSERT INTO orders (cart_id) VALUES ($1)", payload["cart_id"])
    return {"status": "ok"}
'''

SEED_SQL = """\
CREATE TABLE carts (id UUID PRIMARY KEY);
CREATE TABLE orders (id UUID PRIMARY KEY, cart_id UUID);
"""

VALID_MANIFEST = """\
app:
  name: shop-api
  start: uvicorn app:app --host 0.0.0.0 --port 8000
  port: 8000
  healthcheck: /health

database:
  type: postgres
  seed: seed.sql
  observe_tables:
    - carts
    - orders

compare:
  base_ref: main
  target_ref: HEAD
  repo: .

workflows:
  - name: create-cart-and-checkout
    steps:
      - method: POST
        path: /api/carts
        body: {"id": "abc"}
        capture: {"cart_id": "$.cart_id"}
      - method: POST
        path: /api/checkout
        body: {"cart_id": "{cart_id}"}

normalize:
  ignore_fields:
    - "*.created_at"
"""


def _write(root: Path, relative: str, content: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


@pytest.fixture
def sample_repo(tmp_path: Path) -> Path:
    _write(tmp_path, "Dockerfile", DOCKERFILE)
    _write(tmp_path, "app/main.py", APP_PY)
    _write(tmp_path, "seed.sql", SEED_SQL)
    return tmp_path


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
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=self._text)]
        )


def _patch_client(stub: _StubClient):
    return patch("anthropic.Anthropic", return_value=stub)


def _events(logs: list[dict], event: str) -> list[dict]:
    return [entry for entry in logs if entry.get("event") == event]


# --- scan_repo ----------------------------------------------------------------


def test_scan_repo_extracts_everything_from_a_populated_repo(sample_repo: Path):
    context = scan_repo(str(sample_repo))

    assert isinstance(context, RepoContext)
    assert context.dockerfile is not None
    assert "EXPOSE 8000" in context.dockerfile
    assert context.routes == [
        "GET /health",
        "POST /api/carts",
        "POST /api/checkout",
    ]
    assert set(context.tables) == {"carts", "orders"}
    assert context.seed_files == ["seed.sql"]
    assert context.app_port == 8000
    assert context.start_command == "uvicorn app:app --host 0.0.0.0"
    assert context.healthcheck_candidates == ["/health"]


def test_scan_repo_on_empty_directory_returns_empty_context(tmp_path: Path):
    context = scan_repo(str(tmp_path))

    assert context.dockerfile is None
    assert context.routes == []
    assert context.tables == []
    assert context.seed_files == []
    assert context.app_port is None
    assert context.start_command is None
    assert context.healthcheck_candidates == []


def test_scan_repo_skips_vendored_directories(sample_repo: Path):
    # Routes and tables that exist only inside skipped directories must not
    # appear — a dependency's routes are not the app's routes.
    _write(sample_repo, ".venv/lib/site-packages/dep/routes.py", '@app.get("/vendored")\n')
    _write(sample_repo, "node_modules/pkg/index.js", 'app.post("/from-node-modules", h)\n')
    _write(sample_repo, "__pycache__/cached.py", '@app.get("/cached")\n')
    _write(sample_repo, ".venv/dump.sql", "INSERT INTO vendored_table VALUES (1);")

    context = scan_repo(str(sample_repo))

    assert "GET /vendored" not in context.routes
    assert "POST /from-node-modules" not in context.routes
    assert "GET /cached" not in context.routes
    assert "vendored_table" not in context.tables
    assert context.seed_files == ["seed.sql"]


def test_scan_repo_rejects_a_path_that_is_not_a_directory(tmp_path: Path):
    target = tmp_path / "not-a-dir.txt"
    target.write_text("hello")

    with pytest.raises(ManifestGenerationError, match="not a directory"):
        scan_repo(str(target))


def test_scan_repo_reads_lines_that_would_look_like_diff_removals(tmp_path: Path):
    # A route on a line beginning with "-" is ordinary source, but the diff
    # scanners this reuses would drop it as a removed line if it were handed
    # over raw.
    _write(tmp_path, "routes.rb", '-@app.get("/hyphen-leading")\n')

    assert scan_repo(str(tmp_path)).routes == ["GET /hyphen-leading"]


def test_scan_repo_finds_orm_table_declarations(tmp_path: Path):
    _write(
        tmp_path,
        "models.py",
        textwrap.dedent(
            '''\
            class Order(Base):
                __tablename__ = "orders"

            class Payment(models.Model):
                class Meta:
                    db_table = "payments"
            '''
        ),
    )
    _write(tmp_path, "migrate.py", 'op.create_table("refunds")\n')

    assert set(scan_repo(str(tmp_path)).tables) == {"orders", "payments", "refunds"}


def test_scan_repo_finds_seed_files_in_subdirectories(tmp_path: Path):
    _write(tmp_path, "db/seed.sql", "CREATE TABLE carts (id INT);")
    _write(tmp_path, "db/fixtures/extra.sql", "CREATE TABLE orders (id INT);")

    context = scan_repo(str(tmp_path))

    # os.walk yields a directory's own files before descending into it.
    assert context.seed_files == ["db/seed.sql", "db/fixtures/extra.sql"]
    assert set(context.tables) == {"carts", "orders"}


def test_scan_repo_falls_back_to_port_flag_when_expose_is_absent(tmp_path: Path):
    _write(tmp_path, "Dockerfile", 'CMD ["gunicorn", "app:app", "--port", "9001"]\n')

    context = scan_repo(str(tmp_path))

    assert context.app_port == 9001
    assert context.start_command == "gunicorn app:app --port 9001"


def test_scan_repo_falls_back_to_listen_call_when_dockerfile_is_absent(tmp_path: Path):
    _write(tmp_path, "server.js", 'app.get("/ping", h)\napp.listen(3000)\n')

    context = scan_repo(str(tmp_path))

    assert context.dockerfile is None
    assert context.app_port == 3000
    assert context.healthcheck_candidates == ["/ping"]


def test_scan_repo_reads_shell_form_cmd(tmp_path: Path):
    _write(tmp_path, "Dockerfile", "EXPOSE 5000\nCMD python -m app.server\n")

    assert scan_repo(str(tmp_path)).start_command == "python -m app.server"


def test_scan_repo_joins_entrypoint_and_cmd(tmp_path: Path):
    _write(
        tmp_path,
        "Dockerfile",
        'ENTRYPOINT ["uvicorn"]\nCMD ["app:app", "--host", "0.0.0.0"]\n',
    )

    assert scan_repo(str(tmp_path)).start_command == "uvicorn app:app --host 0.0.0.0"


def test_scan_repo_ignores_an_out_of_range_expose_port(tmp_path: Path):
    _write(tmp_path, "Dockerfile", "EXPOSE 99999\n")

    assert scan_repo(str(tmp_path)).app_port is None


# --- healthcheck candidates ---------------------------------------------------


def test_healthcheck_candidates_recognize_common_shapes(tmp_path: Path):
    _write(
        tmp_path,
        "app.py",
        textwrap.dedent(
            '''\
            @app.get("/health")
            def health(): ...

            @app.get("/healthz")
            def healthz(): ...

            @app.get("/api/health")
            def api_health(): ...

            @app.get("/ready")
            def ready(): ...

            @app.get("/ping")
            def ping(): ...

            @app.get("/api/carts")
            def carts(): ...
            '''
        ),
    )

    candidates = scan_repo(str(tmp_path)).healthcheck_candidates

    assert candidates == ["/health", "/healthz", "/api/health", "/ready", "/ping"]
    assert "/api/carts" not in candidates


def test_healthcheck_candidates_are_empty_when_no_route_looks_like_one(tmp_path: Path):
    _write(tmp_path, "app.py", '@app.post("/api/checkout")\ndef checkout(): ...\n')

    assert scan_repo(str(tmp_path)).healthcheck_candidates == []


# --- generate_manifest --------------------------------------------------------


def test_valid_manifest_yaml_is_returned(sample_repo: Path):
    stub = _StubClient(text=VALID_MANIFEST)
    with _patch_client(stub):
        result = generate_manifest(str(sample_repo))

    assert result.strip() == VALID_MANIFEST.strip()


def test_generated_manifest_is_validated(sample_repo: Path):
    stub = _StubClient(text=VALID_MANIFEST)
    with _patch_client(stub), capture_logs() as logs:
        generate_manifest(str(sample_repo))

    # A manifest that validates leaves no complaint behind.
    assert not _events(logs, "generated_manifest_invalid")
    assert _events(logs, "generated_manifest_valid")


def test_invalid_manifest_is_still_returned_with_a_warning(sample_repo: Path):
    # Valid YAML, but `app` is missing every required field.
    broken = "app:\n  name: shop-api\ncompare:\n  base_ref: main\n"
    stub = _StubClient(text=broken)
    with _patch_client(stub), capture_logs() as logs:
        result = generate_manifest(str(sample_repo))

    assert result.strip() == broken.strip()
    (warning,) = _events(logs, "generated_manifest_invalid")
    assert warning["stage"] == "schema"
    assert warning["log_level"] == "warning"


def test_unparseable_yaml_is_still_returned_with_a_warning(sample_repo: Path):
    broken = "app: [unclosed\n  - nope\n"
    stub = _StubClient(text=broken)
    with _patch_client(stub), capture_logs() as logs:
        result = generate_manifest(str(sample_repo))

    assert result.strip() == broken.strip()
    (warning,) = _events(logs, "generated_manifest_invalid")
    assert warning["stage"] == "yaml"


def test_empty_yaml_document_is_still_returned_with_a_warning(sample_repo: Path):
    stub = _StubClient(text="# nothing but a comment\n")
    with _patch_client(stub), capture_logs() as logs:
        result = generate_manifest(str(sample_repo))

    assert result.strip() == "# nothing but a comment"
    (warning,) = _events(logs, "generated_manifest_invalid")
    assert warning["stage"] == "yaml"


def test_code_fences_are_stripped(sample_repo: Path):
    stub = _StubClient(text=f"```yaml\n{VALID_MANIFEST}```")
    with _patch_client(stub):
        result = generate_manifest(str(sample_repo))

    assert result.startswith("app:")
    assert "```" not in result


def test_api_error_raises_manifest_generation_error(sample_repo: Path):
    stub = _StubClient(
        error=anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com"))
    )
    with _patch_client(stub):
        with pytest.raises(ManifestGenerationError, match="Anthropic API call failed"):
            generate_manifest(str(sample_repo))


def test_empty_response_raises_manifest_generation_error(sample_repo: Path):
    stub = _StubClient(text="   ")
    with _patch_client(stub):
        with pytest.raises(ManifestGenerationError, match="no text content"):
            generate_manifest(str(sample_repo))


def test_model_and_max_tokens_match_the_spec(sample_repo: Path):
    stub = _StubClient(text=VALID_MANIFEST)
    with _patch_client(stub):
        generate_manifest(str(sample_repo))

    (call,) = stub.calls
    assert call["model"] == "claude-sonnet-4-6"
    assert call["max_tokens"] == 4096


def test_system_prompt_carries_the_manifest_schema(sample_repo: Path):
    stub = _StubClient(text=VALID_MANIFEST)
    with _patch_client(stub):
        generate_manifest(str(sample_repo))

    (call,) = stub.calls
    system = call["system"]
    # The model is told the real schema, not a prose paraphrase of it.
    assert "observe_tables" in system
    assert "healthcheck" in system
    assert "numeric_tolerance" in system


def test_scan_results_are_sent_as_the_user_message(sample_repo: Path):
    stub = _StubClient(text=VALID_MANIFEST)
    with _patch_client(stub):
        generate_manifest(str(sample_repo))

    (call,) = stub.calls
    (message,) = call["messages"]
    content = message["content"]

    assert message["role"] == "user"
    assert "POST /api/carts" in content
    assert "carts" in content
    assert "seed.sql" in content
    assert "8000" in content
    assert "uvicorn app:app" in content
    assert "/health" in content


def test_refs_and_pr_description_reach_the_prompt(sample_repo: Path):
    stub = _StubClient(text=VALID_MANIFEST)
    with _patch_client(stub):
        generate_manifest(
            str(sample_repo),
            base_ref="release/2.0",
            target_ref="fix/checkout",
            pr_description="Reject checkouts with no city.",
        )

    content = stub.calls[0]["messages"][0]["content"]
    assert "release/2.0" in content
    assert "fix/checkout" in content
    assert "Reject checkouts with no city." in content


def test_default_refs_are_main_and_head(sample_repo: Path):
    stub = _StubClient(text=VALID_MANIFEST)
    with _patch_client(stub):
        generate_manifest(str(sample_repo))

    content = stub.calls[0]["messages"][0]["content"]
    assert "base_ref: main" in content
    assert "target_ref: HEAD" in content


def test_empty_repo_still_produces_a_prompt(tmp_path: Path):
    # Nothing to scan is a reason to say so in the prompt, not to fail before
    # asking — the user still wants a skeleton to edit.
    stub = _StubClient(text=VALID_MANIFEST)
    with _patch_client(stub):
        generate_manifest(str(tmp_path))

    content = stub.calls[0]["messages"][0]["content"]
    assert "none were detected" in content


def test_missing_repo_raises_before_calling_the_api(tmp_path: Path):
    stub = _StubClient(text=VALID_MANIFEST)
    with _patch_client(stub):
        with pytest.raises(ManifestGenerationError, match="not a directory"):
            generate_manifest(str(tmp_path / "nope"))

    assert stub.calls == []
