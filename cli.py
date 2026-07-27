"""BehaviorDiff CLI — wires the engine pipeline together."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import structlog

from ai.classifier import ClassificationError, ClassificationResult, classify_findings
from ai.intent import ChangeIntent, IntentExtractionError, extract_intent
from ai.manifest_gen import ManifestGenerationError, generate_manifest, scan_repo
from ai.scaffold import extract_routes_from_diff, extract_tables_from_diff
from ai.workflow_gen import (
    WorkflowGenerationError,
    WorkflowProposal,
    generate_workflows,
)
from engine.manifest import load_manifest, ManifestError
from engine.orchestrator import Orchestrator, OrchestratorError, RunHandles
from engine.runner import run_workflows, RunnerError
from engine.observers.http import HttpObserver
from engine.observers.postgres import PostgresObserver
from engine.comparator import compare
from web.store import save_run

log = structlog.get_logger(__name__)


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="behaviordiff",
        description="Run two versions of an app and compare their behavior.",
    )
    parser.add_argument(
        "manifest",
        type=Path,
        nargs="?",
        help="Path to the behaviordiff.yaml manifest file. With --init, the path "
             "to the repository to scan instead (defaults to the current directory).",
    )
    parser.add_argument(
        "--init",
        action="store_true",
        help="Scan a repository and write a starter manifest to behaviordiff.yaml. "
             "Requires ANTHROPIC_API_KEY.",
    )
    parser.add_argument(
        "--base-ref",
        default="main",
        help="Git ref for the base version, written into the generated manifest "
             "(--init only). Default: main.",
    )
    parser.add_argument(
        "--target-ref",
        default="HEAD",
        help="Git ref for the target version, written into the generated manifest "
             "(--init only). Default: HEAD.",
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
        "--base-pg-dsn",
        help="Postgres connection string for the base version "
             "(required with --base-url for DB observation).",
    )
    parser.add_argument(
        "--target-pg-dsn",
        help="Postgres connection string for the target version "
             "(required with --target-url for DB observation).",
    )
    parser.add_argument(
        "--diff",
        type=Path,
        help="Path to a file containing the git diff of the change. Enables AI "
             "classification of findings against the change's intent.",
    )
    parser.add_argument(
        "--pr-description",
        help="PR description for the change, used alongside --diff to extract intent.",
    )
    parser.add_argument(
        "--generate-workflows",
        action="store_true",
        help="Propose workflows that exercise the change. Requires --diff.",
    )
    args = parser.parse_args()

    if args.init:
        return _run_init(args)

    if args.manifest is None:
        parser.error("a manifest path is required (or pass --init to generate one)")

    if args.generate_workflows and not args.diff:
        parser.error("--generate-workflows requires --diff")

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
                base_postgres_dsn=args.base_pg_dsn or None,
                target_postgres_dsn=args.target_pg_dsn or None,
            )
        else:
            handles = orchestrator.start()
        log.info("environments_ready", base=handles.base_url, target=handles.target_url)

        # Each version has its own database, so there's no single "base-state vs
        # target-state" snapshot to diff directly — the rows live in separate
        # databases entirely. Instead we snapshot each database before and after
        # the workflows run, diff each version against its own seed to get that
        # version's delta, then compare the two deltas. Since both databases were
        # seeded identically, a change both versions made in the same way cancels
        # out and only real behavioral differences remain.
        observe_db = bool(manifest.database and handles.base_postgres_dsn and handles.target_postgres_dsn)
        if manifest.database and not observe_db:
            log.warning(
                "postgres_observation_skipped",
                reason="a dsn is missing for one or both versions",
            )

        base_pg_before = target_pg_before = None
        if observe_db:
            assert manifest.database is not None
            tables = manifest.database.observe_tables
            base_pg_before = PostgresObserver(handles.base_postgres_dsn).snapshot(tables)
            target_pg_before = PostgresObserver(handles.target_postgres_dsn).snapshot(tables)

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
        if observe_db:
            assert manifest.database is not None
            assert base_pg_before is not None and target_pg_before is not None
            tables = manifest.database.observe_tables
            base_pg_after = PostgresObserver(handles.base_postgres_dsn).snapshot(tables)
            target_pg_after = PostgresObserver(handles.target_postgres_dsn).snapshot(tables)
            base_delta = PostgresObserver.diff(base_pg_before, base_pg_after)
            target_delta = PostgresObserver.diff(target_pg_before, target_pg_after)
            pg_diff = PostgresObserver.compare_deltas(base_delta, target_delta)

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

        # AI layer: intent extraction + finding classification. Advisory only —
        # the findings above are already final, so any failure here degrades to
        # plain output rather than failing the run.
        intent = None
        classification = None
        proposal = None
        diff = _read_diff(args.diff) if args.diff else None
        if diff is not None:
            intent, classification = _classify(
                diff, args.pr_description, result.findings
            )
            if args.generate_workflows:
                proposal = _propose_workflows(diff, args.pr_description)

        # Persist to SQLite for web dashboard
        result_payload = result.model_dump(mode="json")
        intent_payload = intent.model_dump(mode="json") if intent else None
        classification_payload = classification.model_dump(mode="json") if classification else None
        _save_run_id = save_run(
            manifest_path=str(args.manifest),
            app_name=manifest.app.name,
            result=result_payload,
            intent=intent_payload,
            classification=classification_payload,
        )
        log.info("run_persisted", run_id=_save_run_id)

        # Output
        if args.json_output:
            payload = result.model_dump(mode="json")
            if intent is not None:
                payload["intent"] = intent.model_dump(mode="json")
            if classification is not None:
                payload["classification"] = classification.model_dump(mode="json")
            if proposal is not None:
                payload["proposed_workflows"] = proposal.model_dump(mode="json")
            print(json.dumps(payload, indent=2))
        else:
            _print_human(
                result,
                verbose=args.verbose,
                intent=intent,
                classification=classification,
                proposal=proposal,
            )

        return 0 if not result.findings else 1

    except (OrchestratorError, RunnerError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    finally:
        if orchestrator:
            orchestrator.cleanup()


def _run_init(args) -> int:
    """Scan a repository and write a starter manifest.

    Unlike the rest of the AI layer this is not advisory — generating a manifest
    is the entire point of the command, so a missing key or a failed call is an
    error, not a degraded run.
    """
    repo_path = args.manifest or Path(".")

    if not repo_path.is_dir():
        print(f"Error: not a directory: {repo_path}", file=sys.stderr)
        return 1

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print(
            "Error: --init needs ANTHROPIC_API_KEY to be set.\n"
            "It generates the manifest with Claude; there is nothing to fall back on.",
            file=sys.stderr,
        )
        return 1

    # generate_manifest scans the repo itself. Scanning here as well costs one
    # extra pass over local files and lets the user see what context went in
    # before the call is made, rather than only what came back out.
    try:
        context = scan_repo(str(repo_path))
    except ManifestGenerationError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    _print_scan(context, repo_path)

    try:
        manifest_yaml = generate_manifest(
            str(repo_path),
            base_ref=args.base_ref,
            target_ref=args.target_ref,
            pr_description=args.pr_description,
        )
    except ManifestGenerationError as exc:
        print(f"Error: manifest generation failed: {exc}", file=sys.stderr)
        return 2

    out_path = repo_path / "behaviordiff.yaml"
    if out_path.exists():
        fallback = repo_path / "behaviordiff.generated.yaml"
        print(f"\n  Warning: {out_path} already exists, writing to {fallback} instead.")
        out_path = fallback

    try:
        out_path.write_text(manifest_yaml)
    except OSError as exc:
        print(f"Error: could not write {out_path}: {exc}", file=sys.stderr)
        return 2

    print(f"\n  Wrote {out_path}")
    print("  Review it before running — the workflows and observed tables are proposals.\n")
    return 0


def _print_scan(context, repo_path: Path) -> None:
    """Show what the scan found, so the user can judge the input to the model."""
    print(f"\n  Scanned {repo_path}")
    print(f"    Dockerfile:  {'found' if context.dockerfile else 'not found'}")
    print(f"    Port:        {context.app_port if context.app_port is not None else 'not detected'}")
    print(f"    Start:       {context.start_command or 'not detected'}")

    print(f"    Routes:      {len(context.routes)}")
    for route in context.routes:
        print(f"      {route}")

    print(f"    Tables:      {len(context.tables)}")
    for table in context.tables:
        print(f"      {table}")

    if context.seed_files:
        print(f"    SQL files:   {', '.join(context.seed_files)}")
    if context.healthcheck_candidates:
        print(f"    Healthcheck: {', '.join(context.healthcheck_candidates)}")

    if not context.routes:
        print("\n  No routes were detected — the generated manifest will be a skeleton.")
    print()


def _read_diff(diff_path: Path) -> str | None:
    """Read the diff file, or return None if it can't be read.

    An unreadable diff only costs the advisory AI layer, so it is logged and
    skipped rather than failing the comparison that already succeeded.
    """
    try:
        return diff_path.read_text()
    except OSError as exc:
        log.error("diff_read_failed", path=str(diff_path), error=str(exc))
        return None


def _classify(
    diff: str,
    pr_description: str | None,
    findings: list,
) -> tuple[ChangeIntent | None, ClassificationResult | None]:
    """Extract intent from the diff and classify findings against it.

    Returns (None, None) when the AI layer can't run or fails — the engine's
    findings are complete without it, so a missing API key or a bad reply
    should not cost the user their comparison.
    """
    if not os.environ.get("ANTHROPIC_API_KEY"):
        log.warning(
            "ai_classification_skipped",
            reason="ANTHROPIC_API_KEY is not set",
        )
        return None, None

    try:
        intent = extract_intent(diff, pr_description)
    except IntentExtractionError as exc:
        log.error("intent_extraction_failed", error=str(exc))
        return None, None

    try:
        return intent, classify_findings(findings, intent)
    except ClassificationError as exc:
        log.error("classification_failed", error=str(exc))
        return intent, None


def _propose_workflows(
    diff: str,
    pr_description: str | None,
) -> WorkflowProposal | None:
    """Suggest workflows that exercise the change described by the diff.

    Advisory like the rest of the AI layer: returns None on any failure so a bad
    reply costs the suggestion, not the run.
    """
    if not os.environ.get("ANTHROPIC_API_KEY"):
        log.warning(
            "workflow_generation_skipped",
            reason="ANTHROPIC_API_KEY is not set",
        )
        return None

    routes = extract_routes_from_diff(diff)
    tables = extract_tables_from_diff(diff)
    log.info("scaffold_extracted", routes=len(routes), tables=len(tables))

    try:
        return generate_workflows(diff, routes, tables, pr_description)
    except WorkflowGenerationError as exc:
        log.error("workflow_generation_failed", error=str(exc))
        return None


def _print_proposal(proposal: WorkflowProposal) -> None:
    if not proposal.workflows:
        print("\n  No workflows proposed.\n")
    else:
        print(f"\n  {len(proposal.workflows)} proposed workflow(s):\n")
        for workflow in proposal.workflows:
            print(f"  * {workflow.name}")
            print(f"      {workflow.rationale}")
            for step in workflow.steps:
                method = step.get("method", "?")
                path = step.get("path", "?")
                print(f"      {method} {path}")
            print()

    if proposal.coverage_notes:
        print(f"  Coverage: {proposal.coverage_notes}\n")


def _print_human(
    result,
    *,
    verbose: bool = False,
    intent: ChangeIntent | None = None,
    classification: ClassificationResult | None = None,
    proposal: WorkflowProposal | None = None,
) -> None:
    findings = result.findings
    noise = result.noise_summary

    if intent is not None:
        print(f"\n  Intent: {intent.summary}")
        if intent.expected_behavior_changes:
            print("  Expected changes:")
            for change in intent.expected_behavior_changes:
                print(f"    - {change}")

    labels = {}
    if classification is not None:
        labels = {c.finding_index: c for c in classification.classifications}

    if not findings:
        print("\n  No behavioral differences found.\n")
        if noise and noise.total_suppressed > 0:
            print(f"  ({noise.total_suppressed} differences suppressed by normalization)\n")
        if proposal is not None:
            _print_proposal(proposal)
        return

    print(f"\n  {len(findings)} finding(s):\n")
    for i, f in enumerate(findings):
        icon = {"changed": "~", "added": "+", "removed": "-"}.get(f.severity, "?")
        label = labels.get(i)
        prefix = f"[{label.classification}] " if label else ""
        print(f"  [{icon}] {prefix}{f.category} | {f.summary}")
        if f.workflow_name:
            print(f"      workflow: {f.workflow_name}", end="")
            if f.step_index is not None:
                print(f", step {f.step_index}", end="")
            print()
        if label:
            print(f"      {label.reasoning} (confidence {label.confidence:.2f})")
        if verbose:
            print(f"      base:   {f.evidence_base}")
            print(f"      target: {f.evidence_target}")
        print()

    if classification is not None:
        print(f"  Assessment: {classification.summary}\n")

    if noise and noise.total_suppressed > 0:
        print(f"  ({noise.total_suppressed} differences suppressed by normalization)\n")

    if proposal is not None:
        _print_proposal(proposal)

    meta = result.metadata
    print(f"  Ran {meta.total_workflows} workflow(s), {meta.total_steps} step(s) in {meta.duration_seconds}s\n")


if __name__ == "__main__":
    sys.exit(main())
