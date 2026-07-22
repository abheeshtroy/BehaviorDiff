"""Unit tests for the orchestrator, mocking the Docker SDK, subprocess, psycopg,
and httpx so no Docker daemon or real network access is required.
"""

from __future__ import annotations

import subprocess
from typing import Any
from unittest.mock import MagicMock

import docker.errors
import psycopg
import pytest

from engine import orchestrator as orch_module
from engine.manifest import Manifest, parse_manifest
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

        postgres_container = _make_container("pg", 5432, 55432)
        base_container = _make_container("base", 8000, 18000)
        target_container = _make_container("target", 8000, 18001)
        client = _make_client([postgres_container, base_container, target_container])

        orch = Orchestrator(manifest, docker_client=client)

        monkeypatch.setattr(orch_module.subprocess, "run", MagicMock(return_value=MagicMock(returncode=0)))
        _mock_psycopg_ok(monkeypatch)
        monkeypatch.setattr(orch_module.httpx, "get", MagicMock(return_value=MagicMock(status_code=200)))
        _quiet_waits(monkeypatch)

        handles = orch.start()

        assert isinstance(handles, RunHandles)
        assert handles.base_url == "http://localhost:18000"
        assert handles.target_url == "http://localhost:18001"
        assert handles.postgres_dsn is not None
        assert "55432" in handles.postgres_dsn

        assert client.networks.create.call_count == 1
        assert client.containers.run.call_count == 3
        assert client.images.build.call_count == 2

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

        assert handles.postgres_dsn is None
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

    def test_postgres_never_ready_raises_and_cleans_up(self, monkeypatch: pytest.MonkeyPatch) -> None:
        manifest = _manifest(with_database=True)
        postgres_container = _make_container("pg", 5432, 55432)
        client = _make_client([postgres_container])

        orch = Orchestrator(manifest, docker_client=client)
        monkeypatch.setattr(orch_module.subprocess, "run", MagicMock(return_value=MagicMock(returncode=0)))
        monkeypatch.setattr(
            orch_module.psycopg, "connect", MagicMock(side_effect=psycopg.OperationalError("connection refused"))
        )
        _quiet_waits(monkeypatch)

        with pytest.raises(OrchestratorError, match="postgres did not become ready"):
            orch.start()

        postgres_container.remove.assert_called_once_with(force=True)
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
