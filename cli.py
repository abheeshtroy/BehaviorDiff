"""BehaviorDiff CLI — wires the engine pipeline together."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import structlog

from engine.manifest import load_manifest, ManifestError
from engine.orchestrator import Orchestrator, OrchestratorError, RunHandles
from engine.runner import run_workflows, RunnerError
from engine.observers.http import HttpObserver
from engine.observers.postgres import PostgresObserver
from engine.comparator import compare

log = structlog.get_logger(__name__)


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="behaviordiff",
        description="Run two versions of an app and compare their behavior.",
    )
    parser.add_argument(
        "manifest",
        type=Path,
        help="Path to the behaviordiff.yaml manifest file.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Output results as JSON instead of human-readable text.",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Show detailed evidence for each finding.",
    )
    parser.add_argument(
        "--base-url",
        help="URL of the already-running base version (skips orchestration).",
    )
    parser.add_argument(
        "--target-url",
        help="URL of the already-running target version (skips orchestration).",
    )
    parser.add_argument(
        "--pg-dsn",
        help="Postgres connection string (required with --base-url for DB observation).",
    )
    args = parser.parse_args()

    try:
        manifest = load_manifest(args.manifest)
    except ManifestError as exc:
        print(f"Manifest error: {exc}", file=sys.stderr)
        return 1

    log.info("manifest_loaded", app=manifest.app.name, workflows=len(manifest.workflows))

    use_direct = args.base_url and args.target_url
    orchestrator = None if use_direct else Orchestrator(manifest)
    try:
        start = time.monotonic()
        if use_direct:
            handles = RunHandles(
                base_url=args.base_url.rstrip("/"),
                target_url=args.target_url.rstrip("/"),
                postgres_dsn=args.pg_dsn or "",
            )
        else:
            handles = orchestrator.start()
        log.info("environments_ready", base=handles.base_url, target=handles.target_url)

        # Snapshot DB before workflows
        pg_observer = None
        pg_before = None
        if manifest.database and handles.postgres_dsn:
            pg_observer = PostgresObserver(handles.postgres_dsn)
            tables = manifest.database.observe_tables
            pg_before = pg_observer.snapshot(tables)

        # Run workflows
        workflow_results = run_workflows(manifest.workflows, handles)
        log.info("workflows_complete", count=len(workflow_results))

        # Count total steps
        total_steps = sum(len(wr.steps) for wr in workflow_results)

        # Observe HTTP
        http_observer = HttpObserver()
        base_obs, target_obs = http_observer.observe(workflow_results)
        http_diffs = HttpObserver.diff(base_obs, target_obs)

        # Observe Postgres
        pg_diff = None
        if pg_observer and pg_before is not None:
            pg_after = pg_observer.snapshot(tables)
            pg_diff = PostgresObserver.diff(pg_before, pg_after)

        # Observe outbound calls
        outbound_diff = None
        # TODO: wire proxy observer once proxy is started by orchestrator

        duration = time.monotonic() - start

        # Compare
        result = compare(
            http_diffs=http_diffs,
            postgres_diff=pg_diff,
            outbound_diff=outbound_diff,
            normalize_config=manifest.normalize,
            total_workflows=len(workflow_results),
            total_steps=total_steps,
            duration_seconds=round(duration, 2),
        )

        # Output
        if args.json_output:
            print(result.model_dump_json(indent=2))
        else:
            _print_human(result, verbose=args.verbose)

        return 0 if not result.findings else 1

    except (OrchestratorError, RunnerError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    finally:
        if orchestrator:
            orchestrator.cleanup()


def _print_human(result, *, verbose: bool = False) -> None:
    findings = result.findings
    noise = result.noise_summary

    if not findings:
        print("\n  No behavioral differences found.\n")
        if noise and noise.total_suppressed > 0:
            print(f"  ({noise.total_suppressed} differences suppressed by normalization)\n")
        return

    print(f"\n  {len(findings)} finding(s):\n")
    for i, f in enumerate(findings, 1):
        icon = {"changed": "~", "added": "+", "removed": "-"}.get(f.severity, "?")
        print(f"  [{icon}] {f.category} | {f.summary}")
        if f.workflow_name:
            print(f"      workflow: {f.workflow_name}", end="")
            if f.step_index is not None:
                print(f", step {f.step_index}", end="")
            print()
        if verbose:
            print(f"      base:   {f.evidence_base}")
            print(f"      target: {f.evidence_target}")
        print()

    if noise and noise.total_suppressed > 0:
        print(f"  ({noise.total_suppressed} differences suppressed by normalization)\n")

    meta = result.metadata
    print(f"  Ran {meta.total_workflows} workflow(s), {meta.total_steps} step(s) in {meta.duration_seconds}s\n")


if __name__ == "__main__":
    sys.exit(main())
