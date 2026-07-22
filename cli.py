"""BehaviorDiff CLI — wires the engine pipeline together."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import structlog

from engine.manifest import load_manifest, ManifestError
from engine.orchestrator import Orchestrator, OrchestratorError
from engine.runner import run_workflows, RunnerError
from engine.observers.http import HttpObserver
from engine.observers.postgres import PostgresObserver
from engine.observers.proxy import ProxyObserver
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
    args = parser.parse_args()

    try:
        manifest = load_manifest(args.manifest)
    except ManifestError as exc:
        print(f"Manifest error: {exc}", file=sys.stderr)
        return 1

    log.info("manifest_loaded", app=manifest.app.name, workflows=len(manifest.workflows))

    orchestrator = Orchestrator(manifest)
    try:
        start = time.monotonic()
        handles = orchestrator.start()
        log.info("environments_ready", base=handles.base_url, target=handles.target_url)

        # Snapshot DB before workflows
        pg_before_base = None
        pg_before_target = None
        if manifest.database:
            pg_observer = PostgresObserver(handles.postgres_dsn)
            tables = manifest.database.observe_tables
            pg_before_base = pg_observer.snapshot(tables)
            pg_before_target = pg_observer.snapshot(tables)

        # Run workflows
        workflow_results = run_workflows(manifest.workflows, handles)
        log.info("workflows_complete", count=len(workflow_results))

        # Observe HTTP
        http_diffs = HttpObserver.observe_and_diff(workflow_results)

        # Observe Postgres
        pg_diff = None
        if manifest.database and pg_before_base is not None:
            pg_after_base = pg_observer.snapshot(tables)
            pg_after_target = pg_observer.snapshot(tables)
            pg_diff = PostgresObserver.diff(pg_before_base, pg_after_target)

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
            metadata={"duration_s": round(duration, 2)},
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
        orchestrator.cleanup()


def _print_human(result, *, verbose: bool = False) -> None:
    findings = result.findings
    noise = result.noise_summary

    if not findings:
        print("\n  No behavioral differences found.\n")
        if noise:
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

    if noise:
        print(f"  ({noise.total_suppressed} differences suppressed by normalization)\n")


if __name__ == "__main__":
    sys.exit(main())
