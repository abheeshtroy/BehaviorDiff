"""Orchestrator: manages the Docker lifecycle for a comparison run.

Given a parsed Manifest, this stands up an isolated environment: a Docker
network, one Postgres instance per version seeded identically from the
manifest, and two app containers built from the base_ref and target_ref
checkouts. It waits for both apps to report healthy and hands back the
connection details the runner needs. Every resource it creates is tracked
so cleanup() can tear it all back down, even if setup fails partway
through.

Each version gets its own database. Sharing one would let the two versions
write over each other's rows, so a difference in the final state could not
be attributed to either version. Separate databases also mean both sides
start from the same sequence values, so corresponding rows get the same
generated ids and can be matched by primary key when diffing.
"""

from __future__ import annotations

import shlex
import shutil
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import docker
import httpx
import psycopg
import structlog
from docker import DockerClient
from docker.errors import APIError, BuildError, DockerException, ImageNotFound, NotFound
from docker.models.containers import Container
from docker.models.images import Image
from docker.models.networks import Network

from engine.manifest import Manifest

log = structlog.get_logger(__name__)

_POSTGRES_IMAGE = "postgres:16-alpine"
_POSTGRES_USER = "behaviordiff"
_POSTGRES_PASSWORD = "behaviordiff"
_POSTGRES_DB = "behaviordiff"
_POSTGRES_PORT = 5432

_POSTGRES_READY_TIMEOUT_S = 30.0
_POSTGRES_READY_INTERVAL_S = 0.5
_HEALTHCHECK_TIMEOUT_S = 60.0
_HEALTHCHECK_INTERVAL_S = 0.5


class OrchestratorError(Exception):
    """Raised when the orchestrator fails to stand up or tear down a run."""


@dataclass
class RunHandles:
    """Connection details for a live comparison run."""

    base_url: str
    target_url: str
    base_postgres_dsn: str | None
    target_postgres_dsn: str | None


class Orchestrator:
    """Manages the Docker lifecycle for one comparison run of a Manifest."""

    def __init__(self, manifest: Manifest, docker_client: DockerClient | None = None) -> None:
        self.manifest = manifest
        self.client = docker_client or docker.from_env()
        self.run_id = uuid.uuid4().hex[:8]

        self._network: Network | None = None
        self._base_postgres_container: Container | None = None
        self._base_postgres_host_port: int | None = None
        self._target_postgres_container: Container | None = None
        self._target_postgres_host_port: int | None = None
        self._containers: list[Container] = []
        self._images: list[str] = []
        self._workdirs: list[Path] = []

    def start(self) -> RunHandles:
        """Build both versions, start them alongside Postgres, and wait for health.

        Raises OrchestratorError with a clear message on any failure. On
        failure, whatever resources were already created are cleaned up
        before the error propagates.
        """
        try:
            self._create_network()

            base_postgres_dsn: str | None = None
            target_postgres_dsn: str | None = None

            if self.manifest.database is not None:
                # Start both before waiting on either, so the two boots overlap.
                (
                    self._base_postgres_container,
                    self._base_postgres_host_port,
                ) = self._start_postgres("base")
                (
                    self._target_postgres_container,
                    self._target_postgres_host_port,
                ) = self._start_postgres("target")

                for label in ("base", "target"):
                    self._wait_for_postgres(label)
                    self._seed_postgres(label)

                base_postgres_dsn = self._postgres_dsn(host="localhost", port=self._base_postgres_host_port)
                target_postgres_dsn = self._postgres_dsn(host="localhost", port=self._target_postgres_host_port)

            base_container, base_port = self._start_app("base", self.manifest.compare.base_ref)
            target_container, target_port = self._start_app("target", self.manifest.compare.target_ref)

            base_url = f"http://localhost:{base_port}"
            target_url = f"http://localhost:{target_port}"

            self._wait_for_health("base", base_container, base_url)
            self._wait_for_health("target", target_container, target_url)

            log.info("run ready", run_id=self.run_id, base_url=base_url, target_url=target_url)
            return RunHandles(
                base_url=base_url,
                target_url=target_url,
                base_postgres_dsn=base_postgres_dsn,
                target_postgres_dsn=target_postgres_dsn,
            )
        except Exception:
            log.error("run setup failed, cleaning up", run_id=self.run_id)
            self.cleanup()
            raise

    def cleanup(self) -> None:
        """Stop and remove all containers, the network, images, and checkout dirs.

        Best-effort: a failure removing one resource is logged and does not
        prevent the rest from being cleaned up. Safe to call more than once.
        """
        for container in reversed(self._containers):
            self._safe_remove_container(container)
        self._containers.clear()
        self._base_postgres_container = None
        self._base_postgres_host_port = None
        self._target_postgres_container = None
        self._target_postgres_host_port = None

        for tag in reversed(self._images):
            self._safe_remove_image(tag)
        self._images.clear()

        if self._network is not None:
            self._safe_remove_network(self._network)
            self._network = None

        for workdir in self._workdirs:
            shutil.rmtree(workdir, ignore_errors=True)
        self._workdirs.clear()

    # -- network -----------------------------------------------------------

    def _create_network(self) -> None:
        name = f"behaviordiff-{self.run_id}"
        log.info("creating network", name=name)
        try:
            self._network = self.client.networks.create(name, driver="bridge", labels=self._labels())
        except (APIError, DockerException) as exc:
            raise OrchestratorError(f"failed to create docker network {name!r}: {exc}") from exc

    # -- postgres ------------------------------------------------------------

    def _start_postgres(self, label: str) -> tuple[Container, int]:
        """Start the Postgres instance dedicated to one version.

        Returns the container and its published host port; the caller owns
        storing them under the right label.
        """
        assert self._network is not None
        name = f"behaviordiff-{self.run_id}-postgres-{label}"
        log.info("starting postgres", label=label, name=name)
        try:
            container = self.client.containers.run(
                _POSTGRES_IMAGE,
                name=name,
                detach=True,
                network=self._network.name,
                environment={
                    "POSTGRES_USER": _POSTGRES_USER,
                    "POSTGRES_PASSWORD": _POSTGRES_PASSWORD,
                    "POSTGRES_DB": _POSTGRES_DB,
                },
                ports={f"{_POSTGRES_PORT}/tcp": None},
                labels=self._labels(),
            )
        except (APIError, ImageNotFound, DockerException) as exc:
            raise OrchestratorError(f"failed to start {label} postgres container: {exc}") from exc

        # Track for cleanup before anything else can fail.
        self._containers.append(container)
        return container, self._published_port(container, _POSTGRES_PORT)

    def _postgres_handles(self, label: str) -> tuple[Container | None, int | None]:
        """Look up the container and host port belonging to one version."""
        if label == "base":
            return self._base_postgres_container, self._base_postgres_host_port
        if label == "target":
            return self._target_postgres_container, self._target_postgres_host_port
        raise OrchestratorError(f"unknown version label {label!r}: expected 'base' or 'target'")

    def _wait_for_postgres(self, label: str) -> None:
        container, host_port = self._postgres_handles(label)
        assert host_port is not None
        dsn = self._postgres_dsn(host="localhost", port=host_port)
        deadline = time.monotonic() + _POSTGRES_READY_TIMEOUT_S
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                with psycopg.connect(dsn, connect_timeout=2) as conn:
                    conn.execute("SELECT 1")
                return
            except psycopg.OperationalError as exc:
                last_error = exc
                time.sleep(_POSTGRES_READY_INTERVAL_S)

        logs = self._tail_logs(container)
        raise OrchestratorError(
            f"{label} postgres did not become ready within {_POSTGRES_READY_TIMEOUT_S}s "
            f"(last error: {last_error}).\ncontainer logs:\n{logs}"
        )

    def _seed_postgres(self, label: str) -> None:
        assert self.manifest.database is not None
        seed_path = Path(self.manifest.database.seed)
        if not seed_path.is_file():
            raise OrchestratorError(f"seed file not found: {seed_path}")

        _container, host_port = self._postgres_handles(label)
        sql = seed_path.read_text()
        dsn = self._postgres_dsn(host="localhost", port=host_port)
        log.info("seeding postgres", label=label, seed=str(seed_path))
        try:
            with psycopg.connect(dsn, autocommit=True) as conn:
                conn.execute(sql)
        except psycopg.Error as exc:
            raise OrchestratorError(f"failed to run seed SQL {seed_path} for {label}: {exc}") from exc

    def _postgres_dsn(self, host: str, port: int | None) -> str:
        p = port if port is not None else _POSTGRES_PORT
        return f"postgresql://{_POSTGRES_USER}:{_POSTGRES_PASSWORD}@{host}:{p}/{_POSTGRES_DB}"

    # -- app images and containers -------------------------------------------

    def _start_app(self, label: str, ref: str) -> tuple[Container, int]:
        assert self._network is not None
        workdir = Path(tempfile.mkdtemp(prefix=f"behaviordiff-{self.run_id}-{label}-"))
        self._workdirs.append(workdir)

        log.info("checking out ref", label=label, ref=ref)
        self._clone_and_checkout(ref, workdir)

        tag = f"behaviordiff-{self.run_id}-{label}"
        log.info("building image", label=label, ref=ref, tag=tag)
        self._build_image(workdir, self.manifest.app.dockerfile, tag)

        log.info("starting container", label=label, tag=tag)
        try:
            container = self.client.containers.run(
                tag,
                name=tag,
                command=self.manifest.app.start,
                detach=True,
                network=self._network.name,
                environment=self._app_environment(label),
                ports={f"{self.manifest.app.port}/tcp": None},
                labels=self._labels(),
            )
        except (APIError, ImageNotFound, DockerException) as exc:
            raise OrchestratorError(f"failed to start {label} container from {tag!r}: {exc}") from exc

        self._containers.append(container)
        host_port = self._published_port(container, self.manifest.app.port)
        return container, host_port

    def _app_environment(self, label: str) -> dict[str, str]:
        """Point one version's app at the Postgres instance reserved for it."""
        if self.manifest.database is None:
            return {}
        container, _host_port = self._postgres_handles(label)
        if container is None:
            return {}
        # Apps reach postgres over the run's docker network, by container name.
        host = container.name
        dsn = self._postgres_dsn(host=host, port=_POSTGRES_PORT)
        return {
            "DATABASE_URL": dsn,
            "PGHOST": host,
            "PGPORT": str(_POSTGRES_PORT),
            "PGUSER": _POSTGRES_USER,
            "PGPASSWORD": _POSTGRES_PASSWORD,
            "PGDATABASE": _POSTGRES_DB,
        }

    def _clone_and_checkout(self, ref: str, dest: Path) -> None:
        source = self._repo_source()
        self._run_git(["git", "clone", "--quiet", source, str(dest)])
        self._run_git(["git", "checkout", "--quiet", ref], cwd=dest)

    def _repo_source(self) -> str:
        repo = self.manifest.compare.repo
        if "://" in repo or repo.startswith("git@"):
            return repo
        return str(Path(repo).resolve())

    def _run_git(self, args: list[str], cwd: Path | None = None) -> None:
        try:
            subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as exc:
            raise OrchestratorError(f"git command failed: {shlex.join(args)}\n{exc.stderr}") from exc
        except FileNotFoundError as exc:
            raise OrchestratorError("git executable not found on PATH") from exc

    def _build_image(self, path: Path, dockerfile: str, tag: str) -> Image:
        try:
            image, _build_logs = self.client.images.build(path=str(path), dockerfile=dockerfile, tag=tag, rm=True)
        except BuildError as exc:
            raise OrchestratorError(f"docker build failed for {tag!r}: {exc}") from exc
        except APIError as exc:
            raise OrchestratorError(f"docker API error building {tag!r}: {exc}") from exc
        self._images.append(tag)
        return image

    # -- health ---------------------------------------------------------------

    def _wait_for_health(self, label: str, container: Container, base_url: str) -> None:
        url = base_url.rstrip("/") + self.manifest.app.healthcheck
        deadline = time.monotonic() + _HEALTHCHECK_TIMEOUT_S
        last_error: str | None = None

        while time.monotonic() < deadline:
            container.reload()
            if container.status != "running":
                logs = self._tail_logs(container)
                raise OrchestratorError(
                    f"{label} container exited before becoming healthy (status={container.status}).\n"
                    f"container logs:\n{logs}"
                )
            try:
                response = httpx.get(url, timeout=2.0)
                if response.status_code == 200:
                    log.info("healthcheck passed", label=label, url=url)
                    return
                last_error = f"status {response.status_code}"
            except httpx.HTTPError as exc:
                last_error = str(exc)
            time.sleep(_HEALTHCHECK_INTERVAL_S)

        logs = self._tail_logs(container)
        raise OrchestratorError(
            f"{label} healthcheck at {url!r} did not return 200 within {_HEALTHCHECK_TIMEOUT_S}s "
            f"(last error: {last_error}).\ncontainer logs:\n{logs}"
        )

    # -- helpers ---------------------------------------------------------------

    def _published_port(self, container: Container, container_port: int) -> int:
        container.reload()
        bindings = container.ports.get(f"{container_port}/tcp")
        if not bindings:
            raise OrchestratorError(
                f"container {container.name!r} has no published host port for {container_port}/tcp"
            )
        return int(bindings[0]["HostPort"])

    def _tail_logs(self, container: Container | None, tail: int = 50) -> str:
        if container is None:
            return ""
        try:
            return container.logs(tail=tail).decode("utf-8", errors="replace")
        except (APIError, DockerException) as exc:
            return f"<failed to fetch logs: {exc}>"

    def _labels(self) -> dict[str, str]:
        return {"behaviordiff.run_id": self.run_id}

    def _safe_remove_container(self, container: Container) -> None:
        try:
            container.remove(force=True)
        except NotFound:
            pass
        except (APIError, DockerException) as exc:
            log.warning("failed to remove container", container=getattr(container, "name", "?"), error=str(exc))

    def _safe_remove_image(self, tag: str) -> None:
        try:
            self.client.images.remove(tag, force=True)
        except NotFound:
            pass
        except (APIError, DockerException) as exc:
            log.warning("failed to remove image", image=tag, error=str(exc))

    def _safe_remove_network(self, network: Network) -> None:
        try:
            network.remove()
        except NotFound:
            pass
        except (APIError, DockerException) as exc:
            log.warning("failed to remove network", network=getattr(network, "name", "?"), error=str(exc))
