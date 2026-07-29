"""Manifest parser: reads and validates the BehaviorDiff manifest YAML.

The manifest is the single source of truth the engine reads to know how to
build, run, and observe an app under test. Nothing about a specific app is
hardcoded here — every detail comes from the parsed manifest.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    StrictFloat,
    StrictInt,
    field_validator,
    model_validator,
)

_HTTP_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}


class ManifestError(Exception):
    """Raised when a manifest fails to load or validate."""


class StrictModel(BaseModel):
    """Base model that rejects unknown fields so typos fail loudly."""

    model_config = ConfigDict(extra="forbid")


class AppConfig(StrictModel):
    name: str
    start: str
    port: StrictInt = Field(gt=0, le=65535)
    healthcheck: str
    dockerfile: str = "Dockerfile"

    @field_validator("name", "start", "healthcheck")
    @classmethod
    def not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value

    @field_validator("healthcheck")
    @classmethod
    def healthcheck_is_path(cls, value: str) -> str:
        if not value.startswith("/"):
            raise ValueError(f"must start with '/', got {value!r}")
        return value


class DatabaseConfig(StrictModel):
    type: Literal["postgres"]
    seed: str
    observe_tables: list[str] = Field(min_length=1)

    @field_validator("observe_tables")
    @classmethod
    def tables_not_blank(cls, value: list[str]) -> list[str]:
        for table in value:
            if not table.strip():
                raise ValueError("observe_tables entries must not be blank")
        return value


class MockResponse(StrictModel):
    status: StrictInt = Field(ge=100, le=599)
    body: Any = None


class OutboundService(StrictModel):
    name: str
    base_url: str
    mock_responses: dict[str, MockResponse] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def name_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value

    @field_validator("base_url")
    @classmethod
    def base_url_has_scheme(cls, value: str) -> str:
        if not (value.startswith("http://") or value.startswith("https://")):
            raise ValueError(f"must start with http:// or https://, got {value!r}")
        return value

    @field_validator("mock_responses")
    @classmethod
    def mock_response_keys_are_method_and_path(
        cls, value: dict[str, MockResponse]
    ) -> dict[str, MockResponse]:
        for key in value:
            parts = key.split(" ", 1)
            if len(parts) != 2:
                raise ValueError(
                    f"mock_responses key {key!r} must be in the form 'METHOD /path'"
                )
            method, path = parts
            if method.upper() not in _HTTP_METHODS:
                raise ValueError(
                    f"mock_responses key {key!r} has unknown HTTP method {method!r}"
                )
            if not path.startswith("/"):
                raise ValueError(
                    f"mock_responses key {key!r} must have a path starting with '/'"
                )
        return value


class OutboundConfig(StrictModel):
    services: list[OutboundService] = Field(min_length=1)


class CompareConfig(StrictModel):
    base_ref: str
    target_ref: str
    repo: str = "."

    @field_validator("base_ref", "target_ref", "repo")
    @classmethod
    def not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value


class WorkflowStep(StrictModel):
    method: str
    path: str
    body: Any = None
    capture: dict[str, str] | None = None

    @field_validator("method")
    @classmethod
    def method_is_known_verb(cls, value: str) -> str:
        upper = value.upper()
        if upper not in _HTTP_METHODS:
            raise ValueError(
                f"unknown HTTP method {value!r}, expected one of {sorted(_HTTP_METHODS)}"
            )
        return upper

    @field_validator("path")
    @classmethod
    def path_is_absolute(cls, value: str) -> str:
        if not value.startswith("/"):
            raise ValueError(f"must start with '/', got {value!r}")
        return value


class Workflow(StrictModel):
    name: str
    steps: list[WorkflowStep] = Field(min_length=1)

    @field_validator("name")
    @classmethod
    def name_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value


class NormalizeConfig(StrictModel):
    ignore_fields: list[str] = Field(default_factory=list)
    uuid_fields: list[str] = Field(default_factory=list)
    numeric_tolerance: StrictFloat = Field(default=0.0, ge=0.0)
    ignore_row_order: list[str] = Field(default_factory=list)


class Manifest(StrictModel):
    app: AppConfig
    database: DatabaseConfig | None = None
    outbound: OutboundConfig | None = None
    compare: CompareConfig
    workflows: list[Workflow] = Field(min_length=1)
    normalize: NormalizeConfig = Field(default_factory=NormalizeConfig)

    # Where this manifest was read from, when it came from a file. Private so
    # it stays out of the YAML surface (a `source_path:` key in a manifest is
    # still an unknown field) and out of model_dump.
    _source_path: Path | None = PrivateAttr(default=None)

    @model_validator(mode="after")
    def workflow_names_are_unique(self) -> "Manifest":
        seen: set[str] = set()
        for workflow in self.workflows:
            if workflow.name in seen:
                raise ValueError(f"duplicate workflow name: {workflow.name!r}")
            seen.add(workflow.name)
        return self

    @property
    def source_path(self) -> Path | None:
        """The file this manifest was loaded from, or None if parsed from a dict."""
        return self._source_path

    @property
    def base_dir(self) -> Path:
        """Anchor for the relative paths written inside this manifest.

        A path in a manifest describes where something sits relative to the
        manifest, not relative to whoever invoked the engine — otherwise a run
        succeeds or fails based on the caller's working directory. A manifest
        parsed from a dict has no location to anchor to, so it falls back to the
        cwd, which is all it ever had.
        """
        return self._source_path.parent if self._source_path is not None else Path.cwd()

    @property
    def app_dir(self) -> Path:
        """The directory holding the app's own files: its Dockerfile, its seed SQL.

        That is compare.repo, which is where app.dockerfile is resolved from
        (inside a checkout of it). A remote repo URL has no local directory, so
        the manifest's own directory is the only anchor available.
        """
        repo = self.compare.repo
        if _is_remote_repo(repo):
            return self.base_dir
        return (self.base_dir / repo).resolve()

    @property
    def seed_path(self) -> Path | None:
        """Absolute path to the database seed file, or None without a database.

        The seed belongs to the app it seeds and lives with it, alongside the
        Dockerfile — so it resolves against the app directory the same way
        app.dockerfile does.
        """
        if self.database is None:
            return None
        seed = Path(self.database.seed)
        return seed if seed.is_absolute() else (self.app_dir / seed).resolve()


def _is_remote_repo(repo: str) -> bool:
    """True when compare.repo names a remote git URL rather than a local path."""
    return "://" in repo or repo.startswith("git@")


def _format_validation_error(exc: Exception, source: str) -> str:
    from pydantic import ValidationError

    if not isinstance(exc, ValidationError):
        return f"{source}: {exc}"

    lines = [f"{source}: manifest validation failed with {exc.error_count()} error(s):"]
    for error in exc.errors():
        loc = ".".join(str(part) for part in error["loc"]) or "<root>"
        lines.append(f"  - {loc}: {error['msg']}")
    return "\n".join(lines)


def parse_manifest(
    data: dict[str, Any],
    source: str = "<manifest>",
    *,
    source_path: str | Path | None = None,
) -> Manifest:
    """Validate an already-parsed manifest dict into a Manifest model.

    source_path is the file the dict was read from, when there is one; it is
    what the manifest's relative paths are then anchored to.

    Raises ManifestError with a human-readable, field-level message on
    any missing/extra/malformed field.
    """
    if not isinstance(data, dict):
        raise ManifestError(
            f"{source}: expected a YAML mapping at the top level, got {type(data).__name__}"
        )
    try:
        manifest = Manifest.model_validate(data)
    except Exception as exc:  # pydantic.ValidationError
        raise ManifestError(_format_validation_error(exc, source)) from exc

    if source_path is not None:
        manifest._source_path = Path(source_path).resolve()
    return manifest


def load_manifest(path: str | Path) -> Manifest:
    """Load, parse, and validate a manifest YAML file from disk."""
    manifest_path = Path(path)
    if not manifest_path.is_file():
        raise ManifestError(f"manifest file not found: {manifest_path}")

    try:
        raw_text = manifest_path.read_text()
    except OSError as exc:
        raise ManifestError(f"could not read manifest file {manifest_path}: {exc}") from exc

    try:
        data = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise ManifestError(f"{manifest_path}: invalid YAML: {exc}") from exc

    if data is None:
        raise ManifestError(f"{manifest_path}: manifest file is empty")

    return parse_manifest(data, source=str(manifest_path), source_path=manifest_path)
