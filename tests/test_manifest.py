from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

from engine.manifest import ManifestError, Manifest, load_manifest, parse_manifest


def _valid_manifest_dict() -> dict[str, Any]:
    return {
        "app": {
            "name": "my-app",
            "start": "uvicorn app.main:app --host 0.0.0.0 --port 8000",
            "port": 8000,
            "healthcheck": "/health",
        },
        "database": {
            "type": "postgres",
            "seed": "seed.sql",
            "observe_tables": ["orders", "payments", "carts"],
        },
        "outbound": {
            "services": [
                {
                    "name": "payment-provider",
                    "base_url": "https://api.payments.example.com",
                    "mock_responses": {
                        "POST /v1/authorize": {
                            "status": 200,
                            "body": {"auth_id": "mock_001", "status": "approved"},
                        }
                    },
                }
            ]
        },
        "compare": {
            "base_ref": "main",
            "target_ref": "fix/checkout-validation",
            "repo": ".",
        },
        "workflows": [
            {
                "name": "checkout-with-invalid-address",
                "steps": [
                    {
                        "method": "POST",
                        "path": "/api/carts",
                        "body": {"items": [{"sku": "SHOE-42", "qty": 1}]},
                        "capture": {"cart_id": "$.cart_id"},
                    },
                    {
                        "method": "POST",
                        "path": "/api/carts/{cart_id}/discount",
                        "body": {"code": "SAVE10"},
                    },
                    {
                        "method": "POST",
                        "path": "/api/checkout",
                        "body": {"cart_id": "{cart_id}", "address": {"city": "SF"}},
                    },
                ],
            }
        ],
        "normalize": {
            "ignore_fields": ["*.created_at", "*.updated_at", "*.id"],
            "uuid_fields": ["*.cart_id", "*.order_id"],
            "numeric_tolerance": 0.001,
            "ignore_row_order": ["carts", "orders"],
        },
    }


class TestValidManifest:
    def test_parses_full_manifest(self) -> None:
        manifest = parse_manifest(_valid_manifest_dict())

        assert isinstance(manifest, Manifest)
        assert manifest.app.name == "my-app"
        assert manifest.app.port == 8000
        assert manifest.app.dockerfile == "Dockerfile"
        assert manifest.database is not None
        assert manifest.database.observe_tables == ["orders", "payments", "carts"]
        assert manifest.outbound is not None
        assert manifest.outbound.services[0].mock_responses["POST /v1/authorize"].status == 200
        assert manifest.compare.base_ref == "main"
        assert len(manifest.workflows) == 1
        assert manifest.workflows[0].steps[0].method == "POST"
        assert manifest.normalize.numeric_tolerance == 0.001

    def test_minimal_manifest_uses_defaults(self) -> None:
        data = _valid_manifest_dict()
        del data["database"]
        del data["outbound"]
        del data["normalize"]

        manifest = parse_manifest(data)

        assert manifest.database is None
        assert manifest.outbound is None
        assert manifest.app.dockerfile == "Dockerfile"
        assert manifest.normalize.ignore_fields == []
        assert manifest.normalize.numeric_tolerance == 0.0
        assert manifest.compare.repo == "."

    def test_method_is_uppercased(self) -> None:
        data = _valid_manifest_dict()
        data["workflows"][0]["steps"][0]["method"] = "post"

        manifest = parse_manifest(data)

        assert manifest.workflows[0].steps[0].method == "POST"

    def test_loads_from_yaml_file(self, tmp_path: Path) -> None:
        import yaml

        manifest_path = tmp_path / "manifest.yaml"
        manifest_path.write_text(yaml.safe_dump(_valid_manifest_dict()))

        manifest = load_manifest(manifest_path)

        assert manifest.app.name == "my-app"


class TestMissingRequiredFields:
    @pytest.mark.parametrize(
        "path",
        [
            ("app", "name"),
            ("app", "start"),
            ("app", "port"),
            ("app", "healthcheck"),
            ("compare", "base_ref"),
            ("compare", "target_ref"),
        ],
    )
    def test_missing_required_field_raises(self, path: tuple[str, str]) -> None:
        data = _valid_manifest_dict()
        section, field = path
        del data[section][field]

        with pytest.raises(ManifestError, match=field):
            parse_manifest(data)

    def test_missing_app_section_raises(self) -> None:
        data = _valid_manifest_dict()
        del data["app"]

        with pytest.raises(ManifestError, match="app"):
            parse_manifest(data)

    def test_missing_compare_section_raises(self) -> None:
        data = _valid_manifest_dict()
        del data["compare"]

        with pytest.raises(ManifestError, match="compare"):
            parse_manifest(data)

    def test_missing_workflows_raises(self) -> None:
        data = _valid_manifest_dict()
        del data["workflows"]

        with pytest.raises(ManifestError, match="workflows"):
            parse_manifest(data)

    def test_empty_workflows_list_raises(self) -> None:
        data = _valid_manifest_dict()
        data["workflows"] = []

        with pytest.raises(ManifestError):
            parse_manifest(data)

    def test_database_missing_observe_tables_raises(self) -> None:
        data = _valid_manifest_dict()
        del data["database"]["observe_tables"]

        with pytest.raises(ManifestError, match="observe_tables"):
            parse_manifest(data)


class TestWrongTypes:
    def test_port_as_string_raises(self) -> None:
        data = _valid_manifest_dict()
        data["app"]["port"] = "8000"

        with pytest.raises(ManifestError):
            parse_manifest(data)

    def test_port_out_of_range_raises(self) -> None:
        data = _valid_manifest_dict()
        data["app"]["port"] = 99999

        with pytest.raises(ManifestError, match="port"):
            parse_manifest(data)

    def test_workflows_as_dict_raises(self) -> None:
        data = _valid_manifest_dict()
        data["workflows"] = {"name": "oops"}

        with pytest.raises(ManifestError):
            parse_manifest(data)

    def test_observe_tables_as_string_raises(self) -> None:
        data = _valid_manifest_dict()
        data["database"]["observe_tables"] = "orders"

        with pytest.raises(ManifestError):
            parse_manifest(data)

    def test_numeric_tolerance_negative_raises(self) -> None:
        data = _valid_manifest_dict()
        data["normalize"]["numeric_tolerance"] = -1.0

        with pytest.raises(ManifestError, match="numeric_tolerance"):
            parse_manifest(data)


class TestSemanticValidation:
    def test_unknown_database_type_raises(self) -> None:
        data = _valid_manifest_dict()
        data["database"]["type"] = "mysql"

        with pytest.raises(ManifestError, match="type"):
            parse_manifest(data)

    def test_invalid_http_method_raises(self) -> None:
        data = _valid_manifest_dict()
        data["workflows"][0]["steps"][0]["method"] = "FETCH"

        with pytest.raises(ManifestError, match="method"):
            parse_manifest(data)

    def test_path_without_leading_slash_raises(self) -> None:
        data = _valid_manifest_dict()
        data["workflows"][0]["steps"][0]["path"] = "api/carts"

        with pytest.raises(ManifestError):
            parse_manifest(data)

    def test_healthcheck_without_leading_slash_raises(self) -> None:
        data = _valid_manifest_dict()
        data["app"]["healthcheck"] = "health"

        with pytest.raises(ManifestError, match="healthcheck"):
            parse_manifest(data)

    def test_base_url_without_scheme_raises(self) -> None:
        data = _valid_manifest_dict()
        data["outbound"]["services"][0]["base_url"] = "api.payments.example.com"

        with pytest.raises(ManifestError, match="base_url"):
            parse_manifest(data)

    def test_malformed_mock_response_key_raises(self) -> None:
        data = _valid_manifest_dict()
        data["outbound"]["services"][0]["mock_responses"] = {
            "/v1/authorize": {"status": 200, "body": {}}
        }

        with pytest.raises(ManifestError, match="mock_responses"):
            parse_manifest(data)

    def test_mock_response_unknown_method_raises(self) -> None:
        data = _valid_manifest_dict()
        data["outbound"]["services"][0]["mock_responses"] = {
            "FETCH /v1/authorize": {"status": 200, "body": {}}
        }

        with pytest.raises(ManifestError, match="mock_responses"):
            parse_manifest(data)

    def test_blank_app_name_raises(self) -> None:
        data = _valid_manifest_dict()
        data["app"]["name"] = "   "

        with pytest.raises(ManifestError):
            parse_manifest(data)

    def test_duplicate_workflow_names_raise(self) -> None:
        data = _valid_manifest_dict()
        data["workflows"].append(copy.deepcopy(data["workflows"][0]))

        with pytest.raises(ManifestError, match="duplicate"):
            parse_manifest(data)

    def test_empty_steps_list_raises(self) -> None:
        data = _valid_manifest_dict()
        data["workflows"][0]["steps"] = []

        with pytest.raises(ManifestError):
            parse_manifest(data)


class TestUnknownFields:
    def test_unknown_top_level_field_raises(self) -> None:
        data = _valid_manifest_dict()
        data["totally_unknown_section"] = {"foo": "bar"}

        with pytest.raises(ManifestError):
            parse_manifest(data)

    def test_unknown_app_field_raises(self) -> None:
        data = _valid_manifest_dict()
        data["app"]["timeout"] = 30

        with pytest.raises(ManifestError):
            parse_manifest(data)

    def test_unknown_workflow_step_field_raises(self) -> None:
        data = _valid_manifest_dict()
        data["workflows"][0]["steps"][0]["headers"] = {"X-Debug": "1"}

        with pytest.raises(ManifestError):
            parse_manifest(data)


class TestNonMappingInput:
    def test_top_level_list_raises(self) -> None:
        with pytest.raises(ManifestError, match="mapping"):
            parse_manifest([{"app": {}}])  # type: ignore[arg-type]

    def test_top_level_scalar_raises(self) -> None:
        with pytest.raises(ManifestError, match="mapping"):
            parse_manifest("not-a-manifest")  # type: ignore[arg-type]


class TestFileLoading:
    def test_missing_file_raises(self, tmp_path: Path) -> None:
        missing = tmp_path / "does-not-exist.yaml"

        with pytest.raises(ManifestError, match="not found"):
            load_manifest(missing)

    def test_empty_file_raises(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty.yaml"
        empty.write_text("")

        with pytest.raises(ManifestError, match="empty"):
            load_manifest(empty)

    def test_invalid_yaml_raises(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.yaml"
        bad.write_text("app: [unclosed")

        with pytest.raises(ManifestError, match="YAML"):
            load_manifest(bad)


class TestPathAnchoring:
    """Relative paths in a manifest are relative to the manifest, not the cwd."""

    def _written(self, tmp_path: Path, repo: str) -> Path:
        import yaml

        manifest_dir = tmp_path / "manifests"
        manifest_dir.mkdir()
        data = _valid_manifest_dict()
        data["compare"]["repo"] = repo
        path = manifest_dir / "scenario.yaml"
        path.write_text(yaml.safe_dump(data))
        return path

    def test_loaded_manifest_remembers_where_it_came_from(self, tmp_path: Path) -> None:
        path = self._written(tmp_path, ".")

        manifest = load_manifest(path)

        assert manifest.source_path == path.resolve()
        assert manifest.base_dir == path.parent.resolve()

    def test_app_dir_is_the_repo_relative_to_the_manifest(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = self._written(tmp_path, "../shop-api")
        monkeypatch.chdir(tmp_path)

        manifest = load_manifest(path)

        assert manifest.app_dir == (tmp_path / "shop-api").resolve()
        assert manifest.seed_path == (tmp_path / "shop-api" / "seed.sql").resolve()

    def test_remote_repo_falls_back_to_the_manifest_directory(self, tmp_path: Path) -> None:
        path = self._written(tmp_path, "https://github.com/example/shop-api.git")

        manifest = load_manifest(path)

        assert manifest.app_dir == path.parent.resolve()
        assert manifest.seed_path == (path.parent / "seed.sql").resolve()

    def test_source_path_is_not_part_of_the_manifest_schema(self) -> None:
        data = _valid_manifest_dict()
        data["source_path"] = "/somewhere/else/manifest.yaml"

        with pytest.raises(ManifestError, match="source_path"):
            parse_manifest(data)

    def test_dict_manifest_has_no_source_and_dumps_unchanged(self) -> None:
        manifest = parse_manifest(_valid_manifest_dict())

        assert manifest.source_path is None
        assert "source_path" not in manifest.model_dump()
