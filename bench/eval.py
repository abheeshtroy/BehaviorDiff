"""Benchmark harness: run every seeded regression and score what came back.

bench/spec.yaml lists each benchmark manifest alongside the findings the
engine is expected to produce for it. This runs each one through the real
pipeline — the same run_pipeline() the CLI and the dashboard call, with real
containers, a real database and the real comparator — matches the findings it
got against the ones the spec expects, and reports precision and recall over
the whole set.

    python bench/eval.py             # all 17 cases
    python bench/eval.py --case 7    # one case, by its number in the spec

A finding is matched to an expected entry when they agree on the observation
surface (http, postgres, outbound) and the entry's shape is one the finding
has. Matching is one-to-one: a finding matches at most one entry and an entry
is matched by at most one finding, so counts matter and a doubled finding
scores as a false positive. From there:

    true positive   a finding that matched an expected entry
    false positive  a finding that matched none
    false negative  an expected entry no finding matched

    precision = tp / (tp + fp)      how much of what it reported was real
    recall    = tp / (tp + fn)      how much of what was there it reported

The three control cases expect nothing at all; they are what keeps precision
honest, since a tool that reports a difference for a renamed variable is one
nobody will keep reading.

Every case runs the full pipeline, which persists its run to the dashboard
store like any other run — a benchmark pass leaves 17 runs behind there.

Docker must be running, and the demo repository is rebuilt from
demo/shop-api plus demo/variants before the first case so the refs the
manifests name are the ones on disk.
"""

from __future__ import annotations

import argparse
import importlib.util
import logging
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Resolved from this file, never the cwd: the harness behaves the same however
# it is invoked, and the repository root has to be importable before the
# engine imports below can resolve.
BENCH_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BENCH_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import docker  # noqa: E402
import structlog  # noqa: E402
import yaml  # noqa: E402
from docker.errors import APIError, DockerException  # noqa: E402

from engine.comparator import ComparisonResult, Finding  # noqa: E402
from engine.manifest import ManifestError, load_manifest  # noqa: E402
from engine.pipeline import run_pipeline  # noqa: E402

DEFAULT_SPEC = BENCH_DIR / "spec.yaml"

# The shapes an expected entry may name, per surface. Anything else in the
# spec is a typo, and a typo that silently never matches would quietly cost
# recall — so loading rejects it.
SHAPES_BY_SURFACE: dict[str, set[str]] = {
    "http": {"status_change", "body_change", "header_change"},
    "postgres": {"row_inserted", "row_deleted", "row_modified"},
    "outbound": {"call_only_in_base", "call_only_in_target", "call_body_differs"},
}

# A call whose body changed is not one finding: the proxy matches calls by
# method, path and body, so the base call and the target call each end up
# unmatched. "call_body_differs" is satisfied by either half of that pair.
SHAPE_ALIASES: dict[str, set[str]] = {
    "call_body_differs": {"call_only_in_base", "call_only_in_target"},
}


class BenchError(Exception):
    """Raised when the benchmark cannot be run: bad spec, no Docker, no demo repo."""


@dataclass(frozen=True)
class ExpectedFinding:
    """One finding the spec says a case should produce."""

    surface: str
    type: str
    note: str | None = None

    def __str__(self) -> str:
        return f"{self.surface}/{self.type}"


@dataclass(frozen=True)
class Case:
    """One benchmark case: a manifest and what it is expected to surface."""

    number: int
    manifest: Path
    label: str
    expected: tuple[ExpectedFinding, ...]


@dataclass
class CaseResult:
    """What one case actually produced, scored against what it expected."""

    case: Case
    findings: list[Finding] = field(default_factory=list)
    matched: list[tuple[ExpectedFinding, Finding]] = field(default_factory=list)
    false_positives: list[Finding] = field(default_factory=list)
    false_negatives: list[ExpectedFinding] = field(default_factory=list)
    duration_seconds: float = 0.0
    error: str | None = None

    @property
    def true_positives(self) -> int:
        return len(self.matched)

    @property
    def passed(self) -> bool:
        return self.error is None and not self.false_positives and not self.false_negatives


# -- the spec ---------------------------------------------------------------


def load_spec(path: Path) -> list[Case]:
    """Read the benchmark spec into cases, failing loudly on anything malformed."""
    try:
        raw = yaml.safe_load(path.read_text())
    except OSError as exc:
        raise BenchError(f"cannot read the benchmark spec at {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise BenchError(f"{path} is not valid YAML: {exc}") from exc

    if not isinstance(raw, dict) or not isinstance(raw.get("cases"), list):
        raise BenchError(f"{path}: expected a top-level 'cases' list")

    cases: list[Case] = []
    for index, entry in enumerate(raw["cases"], start=1):
        where = f"{path}: case {index}"
        if not isinstance(entry, dict):
            raise BenchError(f"{where}: expected a mapping, got {type(entry).__name__}")

        manifest = entry.get("manifest")
        label = entry.get("label")
        if not isinstance(manifest, str) or not manifest:
            raise BenchError(f"{where}: 'manifest' must be a non-empty string")
        if not isinstance(label, str) or not label:
            raise BenchError(f"{where}: 'label' must be a non-empty string")

        expected_raw = entry.get("expected")
        if expected_raw is None or not isinstance(expected_raw, list):
            raise BenchError(f"{where}: 'expected' must be a list (use [] for a control)")

        # Manifest paths are written relative to the repository root, not to
        # this file — that is how they read in the CLI invocations they mirror.
        manifest_path = (PROJECT_ROOT / manifest).resolve()
        if not manifest_path.is_file():
            raise BenchError(f"{where}: no manifest at {manifest_path}")

        cases.append(
            Case(
                number=index,
                manifest=manifest_path,
                label=label,
                expected=tuple(_parse_expected(item, where, position) for position, item in enumerate(expected_raw, 1)),
            )
        )

    if not cases:
        raise BenchError(f"{path}: the spec has no cases")
    return cases


def _parse_expected(item: Any, where: str, position: int) -> ExpectedFinding:
    at = f"{where}, expected[{position}]"
    if not isinstance(item, dict):
        raise BenchError(f"{at}: expected a mapping, got {type(item).__name__}")

    unknown = set(item) - {"surface", "type", "note"}
    if unknown:
        raise BenchError(f"{at}: unknown key(s) {sorted(unknown)}")

    surface = item.get("surface")
    shape = item.get("type")
    if surface not in SHAPES_BY_SURFACE:
        raise BenchError(f"{at}: 'surface' must be one of {sorted(SHAPES_BY_SURFACE)}, got {surface!r}")
    if shape not in SHAPES_BY_SURFACE[surface]:
        raise BenchError(
            f"{at}: 'type' for surface {surface!r} must be one of "
            f"{sorted(SHAPES_BY_SURFACE[surface])}, got {shape!r}"
        )

    note = item.get("note")
    if note is not None and not isinstance(note, str):
        raise BenchError(f"{at}: 'note' must be a string")
    return ExpectedFinding(surface=surface, type=shape, note=note)


# -- scoring ----------------------------------------------------------------


def finding_shapes(finding: Finding) -> set[str]:
    """The shapes one finding has, as the spec names them.

    Postgres and outbound shapes come from the finding's own fields. HTTP is
    the one surface where a single finding can be several things at once — a
    status change usually moves the body with it — and the only record of
    which of those the comparator decided on (after normalization suppressed
    the rest) is the summary it composed. So the summary is what is read here,
    in the form comparator._compare_http writes it:

        "<METHOD> <path>: status 404 -> 200; body changed; headers changed: x"

    A finding whose summary yields nothing recognizable is given the shape
    "unrecognized", which matches no expected entry and so shows up as a false
    positive rather than being quietly dropped.
    """
    if finding.category == "postgres":
        return {
            "added": {"row_inserted"},
            "removed": {"row_deleted"},
            "changed": {"row_modified"},
        }[finding.severity]

    if finding.category == "outbound":
        return {"removed": {"call_only_in_base"}, "added": {"call_only_in_target"}}[finding.severity]

    if finding.category != "http":
        return {"unrecognized"}

    _, _, detail = finding.summary.partition(": ")
    shapes: set[str] = set()
    for part in (piece.strip() for piece in detail.split(";")):
        if part.startswith("status "):
            shapes.add("status_change")
        elif part == "body changed":
            shapes.add("body_change")
        elif part.startswith("headers changed"):
            shapes.add("header_change")
    return shapes or {"unrecognized"}


def score(case: Case, findings: list[Finding]) -> tuple[
    list[tuple[ExpectedFinding, Finding]],
    list[Finding],
    list[ExpectedFinding],
]:
    """Match findings against expectations one-to-one.

    Returns (matched pairs, unmatched findings, unmatched expectations) —
    true positives, false positives and false negatives respectively.

    Each expectation takes the first unclaimed finding it fits. Two entries
    that would fit the same finding therefore can't both claim it, which is
    what makes a duplicated finding cost precision.
    """
    unclaimed = list(findings)
    matched: list[tuple[ExpectedFinding, Finding]] = []
    missing: list[ExpectedFinding] = []

    for expectation in case.expected:
        wanted = {expectation.type} | SHAPE_ALIASES.get(expectation.type, set())
        hit = next(
            (
                finding
                for finding in unclaimed
                if finding.category == expectation.surface and wanted & finding_shapes(finding)
            ),
            None,
        )
        if hit is None:
            missing.append(expectation)
        else:
            unclaimed.remove(hit)
            matched.append((expectation, hit))

    return matched, unclaimed, missing


# -- running ----------------------------------------------------------------


def require_docker() -> None:
    """Fail before the first build if the Docker daemon isn't there to build with."""
    try:
        client = docker.from_env()
        client.ping()
    except (DockerException, APIError) as exc:
        raise BenchError(
            "Docker is not available, and every benchmark case needs it to build and "
            f"run both versions.\n  {exc}\n"
            "  Start Docker (on macOS, open Docker Desktop) and run this again."
        ) from exc


def rebuild_demo_repo() -> None:
    """Regenerate demo/.demo-repo so the refs the manifests name are current."""
    path = PROJECT_ROOT / "demo" / "build_demo_repo.py"
    spec = importlib.util.spec_from_file_location("build_demo_repo", path)
    if spec is None or spec.loader is None:
        raise BenchError(f"cannot load the demo repository builder at {path}")
    module = importlib.util.module_from_spec(spec)
    # Registered before execution: @dataclass resolves annotations through
    # sys.modules and fails on a module that isn't there yet.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    try:
        module.build(module.DEFAULT_REPO_DIR, module.DEFAULT_BUILD_DIR)
    except module.DemoRepoError as exc:
        raise BenchError(f"could not build the demo repository: {exc}") from exc


def run_case(case: Case) -> CaseResult:
    """Run one case through the pipeline and score whatever it produced."""
    result = CaseResult(case=case)
    started = time.monotonic()

    try:
        manifest = load_manifest(case.manifest)
    except ManifestError as exc:
        result.error = f"manifest error: {exc}"
        result.false_negatives = list(case.expected)
        result.duration_seconds = time.monotonic() - started
        return result

    comparison: ComparisonResult | None = None
    for event in run_pipeline(manifest, manifest_path=str(case.manifest)):
        print(f"      {event.message}", flush=True)
        if event.stage == "done":
            comparison = event.data["models"]["result"]
        elif event.stage == "error":
            result.error = event.data["error"]

    result.duration_seconds = time.monotonic() - started

    if comparison is None:
        # A run that never reached "done" found nothing, so everything the case
        # expected is missing — an errored case costs recall rather than being
        # quietly excluded from the score.
        result.error = result.error or "the run ended without a result"
        result.false_negatives = list(case.expected)
        return result

    result.findings = list(comparison.findings)
    result.matched, result.false_positives, result.false_negatives = score(case, result.findings)
    return result


# -- reporting --------------------------------------------------------------


def print_case_result(result: CaseResult) -> None:
    """Print one case's verdict, and what it got wrong when it got something wrong."""
    verdict = "PASS" if result.passed else "FAIL"
    print(f"      -> {verdict} in {result.duration_seconds:.1f}s: {_counts(result)}")

    if result.error:
        print(f"         error: {result.error}")
    for expectation, finding in result.matched:
        print(f"         matched {expectation}: {finding.summary}")
    for finding in result.false_positives:
        shapes = ", ".join(sorted(finding_shapes(finding)))
        print(f"         unexpected [{finding.category}/{shapes}]: {finding.summary}")
    for expectation in result.false_negatives:
        print(f"         missing {expectation}" + (f" ({expectation.note})" if expectation.note else ""))
    print(flush=True)


def _counts(result: CaseResult) -> str:
    return (
        f"{len(result.case.expected)} expected, {len(result.findings)} found "
        f"(tp {result.true_positives}, fp {len(result.false_positives)}, fn {len(result.false_negatives)})"
    )


def print_summary(results: list[CaseResult]) -> None:
    """Print the per-case table and the precision/recall it adds up to."""
    label_width = max([len(r.case.label) for r in results] + [len("case")])

    header = (
        f"{'#':>3}  {'case':<{label_width}}  {'exp':>3}  {'got':>3}  "
        f"{'tp':>3}  {'fp':>3}  {'fn':>3}  {'time':>7}  result"
    )
    print("\n" + "=" * len(header))
    print("BENCHMARK SUMMARY")
    print("=" * len(header))
    print(header)
    print("-" * len(header))

    for result in results:
        print(
            f"{result.case.number:>3}  {result.case.label:<{label_width}}  "
            f"{len(result.case.expected):>3}  {len(result.findings):>3}  "
            f"{result.true_positives:>3}  {len(result.false_positives):>3}  "
            f"{len(result.false_negatives):>3}  {result.duration_seconds:>6.1f}s  "
            f"{'PASS' if result.passed else 'FAIL'}"
        )
    print("-" * len(header))

    tp = sum(r.true_positives for r in results)
    fp = sum(len(r.false_positives) for r in results)
    fn = sum(len(r.false_negatives) for r in results)
    passed = sum(1 for r in results if r.passed)
    errored = sum(1 for r in results if r.error)

    print(f"\n  cases passed:      {passed}/{len(results)}")
    if errored:
        print(f"  cases errored:     {errored}")
    print(f"  true positives:    {tp}")
    print(f"  false positives:   {fp}")
    print(f"  false negatives:   {fn}")
    print(f"  precision:         {_ratio(tp, tp + fp)}   (tp / tp + fp)")
    print(f"  recall:            {_ratio(tp, tp + fn)}   (tp / tp + fn)")
    print(f"  total time:        {sum(r.duration_seconds for r in results):.1f}s\n")


def _ratio(numerator: int, denominator: int) -> str:
    """Format a ratio, saying so when there was nothing to divide by.

    Precision is undefined when nothing was reported and recall is undefined
    when nothing was expected; printing 1.000 for either would read as a
    perfect score for a run that never scored anything.
    """
    if denominator == 0:
        return "  n/a"
    return f"{numerator / denominator:.3f}"


# -- entry point ------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--case",
        type=int,
        metavar="N",
        help="run only case N from the spec (1-based), for debugging one regression",
    )
    parser.add_argument(
        "--spec",
        type=Path,
        default=DEFAULT_SPEC,
        help=f"benchmark spec to run (default: {DEFAULT_SPEC.relative_to(PROJECT_ROOT)})",
    )
    args = parser.parse_args(argv)

    # The pipeline's own INFO logging would bury the per-case progress this
    # prints; warnings and errors still come through.
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING))

    try:
        cases = load_spec(args.spec)
        if args.case is not None:
            if not 1 <= args.case <= len(cases):
                raise BenchError(f"--case must be between 1 and {len(cases)}, got {args.case}")
            cases = [cases[args.case - 1]]

        require_docker()

        print(f"Rebuilding the demo repository for {len(cases)} case(s)...\n")
        rebuild_demo_repo()
    except BenchError as exc:
        print(f"\nerror: {exc}", file=sys.stderr)
        return 1

    results: list[CaseResult] = []
    for position, case in enumerate(cases, start=1):
        print(f"\n[{position}/{len(cases)}] case {case.number}: {case.label}")
        print(f"      {case.manifest.relative_to(PROJECT_ROOT)}", flush=True)
        result = run_case(case)
        results.append(result)
        print_case_result(result)

    print_summary(results)
    return 0 if all(r.passed for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
