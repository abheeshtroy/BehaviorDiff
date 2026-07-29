"""Tests for demo/build_demo_repo.py, the generator behind the demo scenarios.

The demo is only runnable if the repository it compares actually has the refs
the manifests name, and if each scenario branch differs from main by the one
module it claims to change. Both are cheap to check without Docker, so they are
checked here rather than discovered when someone triggers a run.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from engine.manifest import load_manifest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MANIFEST_DIR = PROJECT_ROOT / "demo" / "manifests"


def _load_builder():
    """Import demo/build_demo_repo.py by path — demo/ is not an importable package."""
    path = PROJECT_ROOT / "demo" / "build_demo_repo.py"
    spec = importlib.util.spec_from_file_location("build_demo_repo", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered before execution: @dataclass resolves annotations through
    # sys.modules and fails on a module that isn't there yet.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


builder = _load_builder()


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    """The demo repository, built once into a temporary directory."""
    root = tmp_path_factory.mktemp("demo")
    repo_dir, build_dir = root / "repo", root / "build"
    builder.build(repo_dir, build_dir)
    return repo_dir, build_dir


class TestGeneratedRepo:
    def test_every_scenario_branch_exists(self, built: tuple[Path, Path]) -> None:
        repo_dir, _ = built
        branches = set(_git(repo_dir, "branch", "--format=%(refname:short)").splitlines())

        assert builder.BASE_BRANCH in branches
        assert {s.branch for s in builder.SCENARIOS} <= branches

    def test_each_branch_changes_only_its_own_overlay(self, built: tuple[Path, Path]) -> None:
        repo_dir, _ = built
        for scenario in builder.SCENARIOS:
            changed = _git(
                repo_dir, "diff", "--name-only", f"{builder.BASE_BRANCH}..{scenario.branch}"
            ).splitlines()
            overlay = builder.VARIANTS_DIR / scenario.overlay
            expected = sorted(
                str(p.relative_to(overlay)) for p in overlay.rglob("*") if p.is_file()
            )
            assert sorted(changed) == expected, scenario.branch

    def test_each_branch_actually_differs_from_main(self, built: tuple[Path, Path]) -> None:
        repo_dir, _ = built
        for scenario in builder.SCENARIOS:
            diff = _git(repo_dir, "diff", f"{builder.BASE_BRANCH}..{scenario.branch}")
            assert diff, f"{scenario.branch} is identical to {builder.BASE_BRANCH}"

    def test_the_app_is_buildable_from_every_ref(self, built: tuple[Path, Path]) -> None:
        # Not a Docker build — just that the pieces the Dockerfile and the
        # manifest's start command need are present on each ref.
        repo_dir, _ = built
        for ref in (builder.BASE_BRANCH, *(s.branch for s in builder.SCENARIOS)):
            tracked = set(_git(repo_dir, "ls-tree", "-r", "--name-only", ref).splitlines())
            assert {"Dockerfile", "requirements.txt", "seed.sql", "app/main.py"} <= tracked, ref

    def test_rebuilding_replaces_the_previous_repository(self, tmp_path: Path) -> None:
        repo_dir, build_dir = tmp_path / "repo", tmp_path / "build"
        builder.build(repo_dir, build_dir)
        stale = repo_dir / "stale.txt"
        stale.write_text("left over from an earlier build")

        builder.build(repo_dir, build_dir)

        assert not stale.exists()

    def test_compose_build_context_exists_for_every_scenario(self, built: tuple[Path, Path]) -> None:
        _, build_dir = built
        for scenario in builder.SCENARIOS:
            assert (build_dir / scenario.slug / "Dockerfile").is_file(), scenario.branch


class TestOverlaySources:
    def test_overlay_touching_an_unknown_path_is_rejected(self, tmp_path: Path) -> None:
        overlay = tmp_path / "overlay"
        (overlay / "app").mkdir(parents=True)
        (overlay / "app" / "not_a_real_module.py").write_text("# nope\n")
        repo = tmp_path / "repo"
        (repo / "app").mkdir(parents=True)

        with pytest.raises(builder.DemoRepoError, match="does not exist"):
            builder._apply_overlay(overlay, repo)

    def test_empty_overlay_is_rejected(self, tmp_path: Path) -> None:
        overlay = tmp_path / "overlay"
        overlay.mkdir()

        with pytest.raises(builder.DemoRepoError, match="empty"):
            builder._apply_overlay(overlay, tmp_path / "repo")


class TestManifestsMatchTheRepo:
    """The manifests and the generator have to agree, or a run dies at checkout."""

    def test_every_manifest_target_ref_is_a_generated_branch(self, built: tuple[Path, Path]) -> None:
        repo_dir, _ = built
        generated = {builder.BASE_BRANCH} | {s.branch for s in builder.SCENARIOS}

        manifests = sorted(MANIFEST_DIR.glob("*.yaml"))
        assert manifests, f"no manifests found in {MANIFEST_DIR}"
        for path in manifests:
            manifest = load_manifest(path)
            assert manifest.compare.base_ref in generated, path.name
            assert manifest.compare.target_ref in generated, path.name

    def test_every_manifest_points_at_the_generated_repository(self) -> None:
        for path in sorted(MANIFEST_DIR.glob("*.yaml")):
            manifest = load_manifest(path)
            assert manifest.app_dir == builder.DEFAULT_REPO_DIR.resolve(), path.name

    def test_observed_tables_all_exist_in_the_seed(self, built: tuple[Path, Path]) -> None:
        repo_dir, _ = built
        seed_sql = (repo_dir / "seed.sql").read_text().lower()
        for path in sorted(MANIFEST_DIR.glob("*.yaml")):
            manifest = load_manifest(path)
            if manifest.database is None:
                continue
            for table in manifest.database.observe_tables:
                assert f"create table if not exists {table}" in seed_sql, f"{path.name}: {table}"
