"""Unit tests for the orchestrator, mocking the Docker SDK, subprocess, psycopg,
and httpx so no Docker daemon or real network access is required.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import docker.errors
import psycopg
import pytest
import yaml

from engine import orchestrator as orch_module
from engine.manifest import Manifest, load_manifest, parse_manifest
from engine.observers.proxy import ProxyObserverError
from engine.orchestrator import Orchestrator, OrchestratorError, RunHandles


def _manifest_dict(with_database: bool = True) -> dict[str, Any]:
    data: dict[str, Any] = {
        "app": {
            "name": "my-app",
            "start": "uvicorn app.main:app --host 0.0.0.0 --port 8000",
            "port": 8000,
            "healthcheck": "/health",
        },
        "compare": {"base_ref": "main", "target_ref": "feature-branch", "repo": "."},
        "workflows": [
            {"name": "smoke", "steps": [{"method": "GET", "path": "/health"}]},
        ],
    }
    if with_database:
        data["database"] = {
            "type": "postgres",
            "seed": "seed.sql",
            "observe_tables": ["orders"],
        }
    return data


def _manifest(with_database: bool = True) -> Manifest:
    return parse_manifest(_manifest_dict(with_database))


def _make_container(name: str, container_port: int, host_port: int, status: str = "running") -> MagicMock:
    container = MagicMock()
    container.name = name
    container.status = status
    container.ports = {f"{container_port}/tcp": [{"HostPort": str(host_port)}]}
    container.logs.return_value = b"some log output"
    return container


def _make_client(run_results: list[MagicMock]) -> MagicMock:
    client = MagicMock()
    network = MagicMock()
    network.name = "behaviordiff-test-network"
    client.networks.create.return_value = network

    results = iter(run_results)
    client.containers.run.side_effect = lambda *a, **k: next(results)
    client.images.build.return_value = (MagicMock(), iter([]))
    return client


def _mock_psycopg_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = MagicMock()
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    monkeypatch.setattr(orch_module.psycopg, "connect", MagicMock(return_value=conn))


def _quiet_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(orch_module.time, "sleep", lambda *_: None)
    monkeypatch.setattr(orch_module, "_HEALTHCHECK_TIMEOUT_S", 0.05)
    monkeypatch.setattr(orch_module, "_HEALTHCHECK_INTERVAL_S", 0.0)
    monkeypatch.setattr(orch_module, "_POSTGRES_READY_TIMEOUT_S", 0.05)
    monkeypatch.setattr(orch_module, "_POSTGRES_READY_INTERVAL_S", 0.0)


class TestHappyPath:
    def test_start_builds_both_versions_and_returns_handles(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "seed.sql").write_text("insert into orders default values;")
        manifest = _manifest(with_database=True)

        base_pg = _make_container("pg-base", 5432, 55432)
        target_pg = _make_container("pg-target", 5432, 55433)
        base_container = _make_container("base", 8000, 18000)
        target_container = _make_container("target", 8000, 18001)
        client = _make_client([base_pg, target_pg, base_container, target_container])

        orch = Orchestrator(manifest, docker_client=client)

        monkeypatch.setattr(orch_module.subprocess, "run", MagicMock(return_value=MagicMock(returncode=0)))
        _mock_psycopg_ok(monkeypatch)
        monkeypatch.setattr(orch_module.httpx, "get", MagicMock(return_value=MagicMock(status_code=200)))
        _quiet_waits(monkeypatch)

        handles = orch.start()

        assert isinstance(handles, RunHandles)
        assert handles.base_url == "http://localhost:18000"
        assert handles.target_url == "http://localhost:18001"

        # Each version gets its own database, on its own host port.
        assert handles.base_postgres_dsn is not None
        assert handles.target_postgres_dsn is not None
        assert "55432" in handles.base_postgres_dsn
        assert "55433" in handles.target_postgres_dsn
        assert handles.base_postgres_dsn != handles.target_postgres_dsn

        assert client.networks.create.call_count == 1
        assert client.containers.run.call_count == 4
        assert client.images.build.call_count == 2

    def test_each_postgres_container_is_named_for_its_version(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "seed.sql").write_text("insert into orders default values;")
        manifest = _manifest(with_database=True)

        client = _make_client(
            [
                _make_container("pg-base", 5432, 55432),
                _make_container("pg-target", 5432, 55433),
                _make_container("base", 8000, 18000),
                _make_container("target", 8000, 18001),
            ]
        )

        orch = Orchestrator(manifest, docker_client=client)

        monkeypatch.setattr(orch_module.subprocess, "run", MagicMock(return_value=MagicMock(returncode=0)))
        _mock_psycopg_ok(monkeypatch)
        monkeypatch.setattr(orch_module.httpx, "get", MagicMock(return_value=MagicMock(status_code=200)))
        _quiet_waits(monkeypatch)

        orch.start()

        names = [call.kwargs["name"] for call in client.containers.run.call_args_list]
        assert names[0] == f"behaviordiff-{orch.run_id}-postgres-base"
        assert names[1] == f"behaviordiff-{orch.run_id}-postgres-target"

    def test_each_app_is_wired_to_its_own_postgres(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "seed.sql").write_text("insert into orders default values;")
        manifest = _manifest(with_database=True)

        client = _make_client(
            [
                _make_container("pg-base", 5432, 55432),
                _make_container("pg-target", 5432, 55433),
                _make_container("base", 8000, 18000),
                _make_container("target", 8000, 18001),
            ]
        )

        orch = Orchestrator(manifest, docker_client=client)

        monkeypatch.setattr(orch_module.subprocess, "run", MagicMock(return_value=MagicMock(returncode=0)))
        _mock_psycopg_ok(monkeypatch)
        monkeypatch.setattr(orch_module.httpx, "get", MagicMock(return_value=MagicMock(status_code=200)))
        _quiet_waits(monkeypatch)

        orch.start()

        # containers.run calls 2 and 3 are the app containers, in base/target order.
        base_env = client.containers.run.call_args_list[2].kwargs["environment"]
        target_env = client.containers.run.call_args_list[3].kwargs["environment"]

        assert base_env["PGHOST"] == "pg-base"
        assert target_env["PGHOST"] == "pg-target"
        assert "pg-base" in base_env["DATABASE_URL"]
        assert "pg-target" in target_env["DATABASE_URL"]
        assert base_env["DATABASE_URL"] != target_env["DATABASE_URL"]

    def test_start_without_database_skips_postgres(self, monkeypatch: pytest.MonkeyPatch) -> None:
        manifest = _manifest(with_database=False)

        base_container = _make_container("base", 8000, 18000)
        target_container = _make_container("target", 8000, 18001)
        client = _make_client([base_container, target_container])

        orch = Orchestrator(manifest, docker_client=client)

        monkeypatch.setattr(orch_module.subprocess, "run", MagicMock(return_value=MagicMock(returncode=0)))
        connect_mock = MagicMock()
        monkeypatch.setattr(orch_module.psycopg, "connect", connect_mock)
        monkeypatch.setattr(orch_module.httpx, "get", MagicMock(return_value=MagicMock(status_code=200)))
        _quiet_waits(monkeypatch)

        handles = orch.start()

        assert handles.base_postgres_dsn is None
        assert handles.target_postgres_dsn is None
        connect_mock.assert_not_called()
        assert client.containers.run.call_count == 2


class TestFailureModes:
    def test_git_clone_failure_raises_orchestrator_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        manifest = _manifest(with_database=False)
        client = _make_client([])

        orch = Orchestrator(manifest, docker_client=client)

        failing_run = MagicMock(
            side_effect=subprocess.CalledProcessError(1, ["git", "clone"], stderr="fatal: repository not found")
        )
        monkeypatch.setattr(orch_module.subprocess, "run", failing_run)
        _quiet_waits(monkeypatch)

        with pytest.raises(OrchestratorError, match="git command failed"):
            orch.start()

    def test_docker_build_failure_raises_orchestrator_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        manifest = _manifest(with_database=False)
        client = _make_client([])
        client.images.build.side_effect = docker.errors.BuildError(reason="bad Dockerfile", build_log=[])

        orch = Orchestrator(manifest, docker_client=client)
        monkeypatch.setattr(orch_module.subprocess, "run", MagicMock(return_value=MagicMock(returncode=0)))
        _quiet_waits(monkeypatch)

        with pytest.raises(OrchestratorError, match="docker build failed"):
            orch.start()

    def test_postgres_never_ready_raises_and_cleans_up_both(self, monkeypatch: pytest.MonkeyPatch) -> None:
        manifest = _manifest(with_database=True)
        base_pg = _make_container("pg-base", 5432, 55432)
        target_pg = _make_container("pg-target", 5432, 55433)
        client = _make_client([base_pg, target_pg])

        orch = Orchestrator(manifest, docker_client=client)
        monkeypatch.setattr(orch_module.subprocess, "run", MagicMock(return_value=MagicMock(returncode=0)))
        monkeypatch.setattr(
            orch_module.psycopg, "connect", MagicMock(side_effect=psycopg.OperationalError("connection refused"))
        )
        _quiet_waits(monkeypatch)

        with pytest.raises(OrchestratorError, match="base postgres did not become ready"):
            orch.start()

        # Both databases are started before either is waited on, so both
        # must be torn down even though only the first one was checked.
        base_pg.remove.assert_called_once_with(force=True)
        target_pg.remove.assert_called_once_with(force=True)
        client.networks.create.return_value.remove.assert_called_once()

    def test_healthcheck_timeout_raises_and_cleans_up(self, monkeypatch: pytest.MonkeyPatch) -> None:
        manifest = _manifest(with_database=False)
        base_container = _make_container("base", 8000, 18000)
        target_container = _make_container("target", 8000, 18001)
        client = _make_client([base_container, target_container])

        orch = Orchestrator(manifest, docker_client=client)
        monkeypatch.setattr(orch_module.subprocess, "run", MagicMock(return_value=MagicMock(returncode=0)))
        monkeypatch.setattr(orch_module.httpx, "get", MagicMock(return_value=MagicMock(status_code=503)))
        _quiet_waits(monkeypatch)

        with pytest.raises(OrchestratorError, match="healthcheck"):
            orch.start()

        base_container.remove.assert_called_once_with(force=True)
        target_container.remove.assert_called_once_with(force=True)
        assert client.images.remove.call_count == 2
        client.networks.create.return_value.remove.assert_called_once()

    def test_container_exits_before_healthy_raises_with_logs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        manifest = _manifest(with_database=False)
        base_container = _make_container("base", 8000, 18000, status="exited")
        target_container = _make_container("target", 8000, 18001)
        client = _make_client([base_container, target_container])

        orch = Orchestrator(manifest, docker_client=client)
        monkeypatch.setattr(orch_module.subprocess, "run", MagicMock(return_value=MagicMock(returncode=0)))
        monkeypatch.setattr(orch_module.httpx, "get", MagicMock(return_value=MagicMock(status_code=200)))
        _quiet_waits(monkeypatch)

        with pytest.raises(OrchestratorError, match="exited before becoming healthy"):
            orch.start()

    def test_missing_published_port_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        manifest = _manifest(with_database=False)
        base_container = _make_container("base", 8000, 18000)
        base_container.ports = {}
        client = _make_client([base_container])

        orch = Orchestrator(manifest, docker_client=client)
        monkeypatch.setattr(orch_module.subprocess, "run", MagicMock(return_value=MagicMock(returncode=0)))
        _quiet_waits(monkeypatch)

        with pytest.raises(OrchestratorError, match="no published host port"):
            orch.start()


class TestSeedResolution:
    """database.seed resolves against the app directory, never the caller's cwd.

    The regression: seed was read as Path(manifest.database.seed), so a manifest
    saying `seed: seed.sql` only worked when the engine happened to be invoked
    from the one directory holding that file. Every run from anywhere else died
    with "seed file not found" before starting a container.
    """

    SEED_SQL = "insert into orders default values;"

    def _project(self, tmp_path: Path) -> Path:
        """A manifest in one directory, the app (and its seed) in a sibling one."""
        app_dir = tmp_path / "project" / "shop-api"
        app_dir.mkdir(parents=True)
        (app_dir / "seed.sql").write_text(self.SEED_SQL)
        (app_dir / "Dockerfile").write_text("FROM python:3.12-slim\n")

        manifest_dir = tmp_path / "project" / "manifests"
        manifest_dir.mkdir(parents=True)
        manifest_path = manifest_dir / "scenario.yaml"
        data = _manifest_dict(with_database=True)
        data["compare"]["repo"] = "../shop-api"
        manifest_path.write_text(yaml.safe_dump(data))

        (tmp_path / "elsewhere").mkdir()
        return manifest_path

    def _start(self, manifest: Manifest, monkeypatch: pytest.MonkeyPatch) -> MagicMock:
        """Run start() with Docker/psycopg/httpx mocked; hands back the psycopg conn."""
        client = _make_client(
            [
                _make_container("pg-base", 5432, 55432),
                _make_container("pg-target", 5432, 55433),
                _make_container("base", 8000, 18000),
                _make_container("target", 8000, 18001),
            ]
        )
        conn = MagicMock()
        conn.__enter__.return_value = conn
        conn.__exit__.return_value = False
        monkeypatch.setattr(orch_module.psycopg, "connect", MagicMock(return_value=conn))
        monkeypatch.setattr(orch_module.subprocess, "run", MagicMock(return_value=MagicMock(returncode=0)))
        monkeypatch.setattr(orch_module.httpx, "get", MagicMock(return_value=MagicMock(status_code=200)))
        _quiet_waits(monkeypatch)

        orch = Orchestrator(manifest, docker_client=client)
        self.client = client
        orch.start()
        return conn

    def test_seed_is_found_from_an_unrelated_cwd(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        manifest_path = self._project(tmp_path)
        # Nothing about the cwd points at the app: no seed.sql, no manifest.
        monkeypatch.chdir(tmp_path / "elsewhere")
        manifest = load_manifest(manifest_path)

        conn = self._start(manifest, monkeypatch)

        # Both databases were seeded, from the app directory's seed file.
        executed = [call.args[0] for call in conn.execute.call_args_list]
        assert executed.count(self.SEED_SQL) == 2

    def test_repo_checkout_uses_the_same_anchor_as_the_seed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        manifest_path = self._project(tmp_path)
        monkeypatch.chdir(tmp_path / "elsewhere")
        manifest = load_manifest(manifest_path)

        self._start(manifest, monkeypatch)

        app_dir = (tmp_path / "project" / "shop-api").resolve()
        clones = [
            call.args[0]
            for call in orch_module.subprocess.run.call_args_list
            if call.args[0][:2] == ["git", "clone"]
        ]
        assert len(clones) == 2
        assert all(app_dir.samefile(args[3]) for args in clones)

    def test_missing_seed_names_the_path_it_looked_in(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        manifest_path = self._project(tmp_path)
        (tmp_path / "project" / "shop-api" / "seed.sql").unlink()
        monkeypatch.chdir(tmp_path / "elsewhere")
        manifest = load_manifest(manifest_path)

        # A cwd-local seed file must not rescue a manifest pointing elsewhere.
        (tmp_path / "elsewhere" / "seed.sql").write_text(self.SEED_SQL)

        with pytest.raises(OrchestratorError, match="seed file not found") as excinfo:
            self._start(manifest, monkeypatch)

        message = str(excinfo.value)
        assert str((tmp_path / "project" / "shop-api").resolve()) in message

    def test_absolute_seed_path_is_taken_as_given(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        manifest_path = self._project(tmp_path)
        absolute_seed = tmp_path / "somewhere-else" / "custom-seed.sql"
        absolute_seed.parent.mkdir()
        absolute_seed.write_text(self.SEED_SQL)

        data = yaml.safe_load(manifest_path.read_text())
        data["database"]["seed"] = str(absolute_seed)
        manifest_path.write_text(yaml.safe_dump(data))

        monkeypatch.chdir(tmp_path / "elsewhere")
        manifest = load_manifest(manifest_path)

        assert manifest.seed_path == absolute_seed
        conn = self._start(manifest, monkeypatch)
        assert [call.args[0] for call in conn.execute.call_args_list].count(self.SEED_SQL) == 2

    def test_manifest_from_a_dict_still_anchors_on_the_cwd(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # No source file to anchor to, so the cwd is all there is — the
        # behaviour a caller that hands over a dict has always had.
        monkeypatch.chdir(tmp_path)
        (tmp_path / "seed.sql").write_text(self.SEED_SQL)
        manifest = _manifest(with_database=True)

        assert manifest.source_path is None
        assert manifest.seed_path == (tmp_path / "seed.sql").resolve()


class TestOutboundProxies:
    """Each version's outbound calls go to a proxy of its own, and are recorded there."""

    def _manifest_with_outbound(self) -> Manifest:
        data = _manifest_dict(with_database=False)
        data["outbound"] = {
            "services": [
                {
                    "name": "payment-provider",
                    "base_url": "https://api.payments.example.com",
                    "mock_responses": {"POST /v1/authorize": {"status": 200, "body": {"ok": True}}},
                }
            ]
        }
        return parse_manifest(data)

    def _start(self, manifest: Manifest, monkeypatch: pytest.MonkeyPatch) -> tuple[Orchestrator, MagicMock]:
        client = _make_client(
            [
                _make_container("base", 8000, 18000),
                _make_container("target", 8000, 18001),
            ]
        )
        orch = Orchestrator(manifest, docker_client=client)
        monkeypatch.setattr(orch_module.subprocess, "run", MagicMock(return_value=MagicMock(returncode=0)))
        monkeypatch.setattr(orch_module.httpx, "get", MagicMock(return_value=MagicMock(status_code=200)))
        _quiet_waits(monkeypatch)
        return orch, client

    def test_each_version_gets_its_own_proxy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        orch, _client = self._start(self._manifest_with_outbound(), monkeypatch)
        try:
            handles = orch.start()

            assert len(handles.base_proxy_observers) == 1
            assert len(handles.target_proxy_observers) == 1
            base_proxy = handles.base_proxy_observers[0]
            target_proxy = handles.target_proxy_observers[0]
            assert base_proxy.service.name == "payment-provider"
            # Separate ports: a shared proxy could not attribute a recorded
            # call to the version that made it.
            assert base_proxy.address[1] != target_proxy.address[1]
        finally:
            orch.cleanup()

    def test_each_app_is_pointed_at_its_own_proxy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        orch, client = self._start(self._manifest_with_outbound(), monkeypatch)
        try:
            handles = orch.start()

            base_env = client.containers.run.call_args_list[0].kwargs["environment"]
            target_env = client.containers.run.call_args_list[1].kwargs["environment"]
            var = "OUTBOUND_PAYMENT_PROVIDER_URL"

            base_port = handles.base_proxy_observers[0].address[1]
            target_port = handles.target_proxy_observers[0].address[1]
            assert base_env[var] == f"http://host.docker.internal:{base_port}"
            assert target_env[var] == f"http://host.docker.internal:{target_port}"
        finally:
            orch.cleanup()

    def test_no_outbound_config_starts_no_proxies(self, monkeypatch: pytest.MonkeyPatch) -> None:
        orch, client = self._start(_manifest(with_database=False), monkeypatch)
        try:
            handles = orch.start()

            assert handles.base_proxy_observers == []
            assert handles.target_proxy_observers == []
            env = client.containers.run.call_args_list[0].kwargs["environment"]
            assert not [key for key in env if key.startswith("OUTBOUND_")]
        finally:
            orch.cleanup()

    def test_cleanup_stops_every_proxy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        orch, _client = self._start(self._manifest_with_outbound(), monkeypatch)
        handles = orch.start()
        proxies = [*handles.base_proxy_observers, *handles.target_proxy_observers]

        orch.cleanup()

        for proxy in proxies:
            # address raises once the server is closed, which is how a stopped
            # proxy is observable from outside.
            with pytest.raises(ProxyObserverError):
                proxy.address

    def test_cleanup_tolerates_a_proxy_that_fails_to_stop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        manifest = _manifest(with_database=False)
        orch = Orchestrator(manifest, docker_client=_make_client([]))

        broken = MagicMock()
        broken.service.name = "payment-provider"
        broken.stop.side_effect = OSError("socket already closed")
        healthy = MagicMock()
        healthy.service.name = "payment-provider"
        orch._base_proxy_observers = [broken]
        orch._target_proxy_observers = [healthy]

        orch.cleanup()

        healthy.stop.assert_called_once()
        assert orch._base_proxy_observers == []


class TestCleanup:
    def test_cleanup_removes_all_tracked_resources_and_is_idempotent(self) -> None:
        manifest = _manifest(with_database=False)
        client = _make_client([])
        orch = Orchestrator(manifest, docker_client=client)

        container_a = MagicMock()
        container_b = MagicMock()
        network = MagicMock()
        orch._containers = [container_a, container_b]
        orch._images = ["behaviordiff-tag-a", "behaviordiff-tag-b"]
        orch._network = network

        orch.cleanup()

        container_a.remove.assert_called_once_with(force=True)
        container_b.remove.assert_called_once_with(force=True)
        assert client.images.remove.call_count == 2
        network.remove.assert_called_once()

        orch.cleanup()

        container_a.remove.assert_called_once()
        container_b.remove.assert_called_once()
        assert client.images.remove.call_count == 2
        network.remove.assert_called_once()

    def test_cleanup_tolerates_already_removed_resources(self) -> None:
        manifest = _manifest(with_database=False)
        client = _make_client([])
        orch = Orchestrator(manifest, docker_client=client)

        container = MagicMock()
        container.remove.side_effect = docker.errors.NotFound("already gone")
        orch._containers = [container]

        orch.cleanup()

        container.remove.assert_called_once_with(force=True)
