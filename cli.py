"""BehaviorDiff CLI — wires the engine pipeline together."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import structlog

from ai.classifier import ClassificationResult
from ai.intent import ChangeIntent
from ai.manifest_gen import ManifestGenerationError, generate_manifest, scan_repo
from ai.workflow_gen import WorkflowProposal
from engine.manifest import load_manifest, ManifestError
from engine.pipeline import RunEvent, run_pipeline
from engine.report import write_report

log = structlog.get_logger(__name__)


class _CurrentStream:
    """A structlog stream that follows the process's current stdio object."""

    def __init__(self, name: str) -> None:
        self.name = name

    def write(self, value: str) -> int:
        return getattr(sys, self.name).write(value)

    def flush(self) -> None:
        getattr(sys, self.name).flush()


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
        help="Git ref for the base version at runtime; with --init, use it in the "
             "generated manifest. Without --init, overrides compare.base_ref in memory.",
    )
    parser.add_argument(
        "--target-ref",
        help="Git ref for the target version at runtime; with --init, use it in the "
             "generated manifest. Without --init, overrides compare.target_ref in memory.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Output results as JSON instead of human-readable text.",
    )
    parser.add_argument(
        "--report",
        type=Path,
        help="Write a self-contained offline BehaviorDiff Review HTML file.",
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

    # stdout is a machine-readable channel in JSON mode. Keep all structlog
    # diagnostics on stderr so the result remains exactly one JSON document.
    # Explicitly select stdout for the human-readable mode to preserve the
    # CLI's existing logging behavior when main() is called more than once in
    # the same process (for example, by an embedding application or tests).
    structlog.configure(
        logger_factory=structlog.PrintLoggerFactory(
            file=_CurrentStream("stderr" if args.json_output else "stdout")
        )
    )

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
        return 2

    # Runtime overrides deliberately affect only this parsed model. The YAML is
    # the user's source of truth and must remain unchanged by a comparison.
    if args.base_ref is not None:
        manifest.compare.base_ref = args.base_ref
    if args.target_ref is not None:
        manifest.compare.target_ref = args.target_ref

    log.info("manifest_loaded", app=manifest.app.name, workflows=len(manifest.workflows))

    # The pipeline owns the run; this loop only turns its events back into the
    # log lines the CLI has always printed, and renders the terminal event.
    terminal: RunEvent | None = None
    for event in run_pipeline(
        manifest,
        manifest_path=str(args.manifest),
        base_url=args.base_url,
        target_url=args.target_url,
        base_pg_dsn=args.base_pg_dsn,
        target_pg_dsn=args.target_pg_dsn,
        diff_text=_read_diff(args.diff) if args.diff else None,
        pr_description=args.pr_description,
        generate_workflows=args.generate_workflows,
    ):
        if event.stage == "environments_ready":
            log.info(
                "environments_ready",
                base=event.data["base_url"],
                target=event.data["target_url"],
            )
        elif event.stage == "postgres_observation_skipped":
            log.warning("postgres_observation_skipped", reason=event.data["reason"])
        elif event.stage == "workflows_complete":
            log.info("workflows_complete", count=event.data["count"])
        elif event.stage == "done":
            log.info("run_persisted", run_id=event.data["run_id"])
            terminal = event
        elif event.stage == "error":
            terminal = event

    if terminal is None:
        print("Error: the run ended without a result", file=sys.stderr)
        return 2

    if terminal.stage == "error":
        return _report_error(terminal, args)

    return _report_result(terminal, args)


def _report_error(event: RunEvent, args=None) -> int:
    """Render a failed run.

    Orchestrator and runner failures are the CLI's own error message and exit
    code 2. Anything else is a bug in the engine, so the original exception is
    re-raised rather than reported as an ordinary failed comparison.
    """
    if not event.data["expected"]:
        raise event.data["exception"]
    if getattr(args, "report", None):
        _write_report(args.report, {"error": event.data["error"]})
    print(f"Error: {event.data['error']}", file=sys.stderr)
    return 2


def _report_result(event: RunEvent, args) -> int:
    """Render a completed run and return the CLI's exit code."""
    models = event.data["models"]
    result = models["result"]
    intent = models["intent"]
    classification = models["classification"]
    proposal = models["proposal"]

    payload = _result_payload(event, intent, classification, proposal)
    if getattr(args, "report", None):
        _write_report(args.report, payload)

    if args.json_output:
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


def _result_payload(event: RunEvent, intent, classification, proposal) -> dict:
    """The one structured result shared by --json and --report."""
    payload = dict(event.data["result"])
    if intent is not None:
        payload["intent"] = event.data["intent"]
    if classification is not None:
        payload["classification"] = event.data["classification"]
    if proposal is not None:
        payload["proposed_workflows"] = event.data["proposed_workflows"]
    return payload


def _write_report(path: Path, payload: dict) -> None:
    try:
        write_report(path, payload)
    except OSError as exc:
        # A review artifact must not make the underlying comparison look clean.
        print(f"Warning: could not write report {path}: {exc}", file=sys.stderr)


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
            base_ref=args.base_ref or "main",
            target_ref=args.target_ref or "HEAD",
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
