/**
 * A run's event stream and its findings, woven into one chronological list.
 *
 * The events say what happened and when; the findings say which of it came out
 * different between the two versions. Neither alone answers "where in the run
 * did this go wrong" — that is what the sequence view is for.
 *
 * Kept out of the component so the ordering and the attribution can be tested
 * without a DOM, the way the rest of lib/ is.
 */

/**
 * Stages worth a row. Standing the environments up and writing the run to the
 * store are things the harness did, not things the app did, so they are left
 * out; an unrecognised stage is dropped for the same reason rather than
 * rendered as an unexplained row.
 */
const SHOWN_STAGES = new Set([
  "workflow_started",
  "workflow_completed",
  "observing_http",
  "observing_postgres",
  "comparing",
  "classifying",
  "done",
]);

/** Stages that are the engine talking about the run rather than the run itself. */
const ENGINE_STAGES = new Set(["comparing", "classifying", "done"]);

const ICON = {
  http: "→",
  postgres: "◆",
  outbound: "⟐",
  engine: "⊙",
};

const STAGE_ICON = {
  workflow_started: ICON.http,
  workflow_completed: ICON.http,
  observing_http: ICON.http,
  observing_postgres: ICON.postgres,
  comparing: ICON.engine,
  classifying: ICON.engine,
  done: ICON.engine,
};

/** An offset from the start of the run, at the precision a reader can use. */
export function formatElapsed(ms) {
  if (ms <= 0) return "0ms";
  if (ms < 1000) return `+${Math.round(ms)}ms`;
  return `+${(ms / 1000).toFixed(1)}s`;
}

function label(event) {
  const data = event.data ?? {};
  switch (event.stage) {
    case "workflow_started":
      // data.name is what the pipeline emits; the alias keeps a hand-written
      // stream from rendering as "workflow".
      return data.name ?? data.workflow_name ?? "workflow";
    case "workflow_completed":
      return `Completed: ${data.name ?? data.workflow_name ?? "workflow"}`;
    case "observing_postgres":
      return `DB snapshot (${data.phase ?? "unknown"})`;
    case "observing_http":
      return "HTTP comparison";
    case "comparing":
      return "Comparing observations";
    case "classifying":
      return "Classifying findings";
    case "done":
      return "Run complete";
    default:
      return event.stage;
  }
}

function workflowOf(event) {
  const data = event.data ?? {};
  return data.name ?? data.workflow_name ?? null;
}

/**
 * Findings grouped by the row they belong to.
 *
 * A finding naming a workflow lands on that workflow's completion — the point
 * in the run by which the difference existed. The comparator leaves
 * workflow_name unset on Postgres and outbound findings (it diffs whole-run
 * database deltas, not per-workflow ones), so those land on the closing DB
 * snapshot instead of vanishing from a view whose whole job is to show where
 * things went wrong.
 */
function groupFindings(findings) {
  const byWorkflow = new Map();
  const runLevel = [];

  for (const finding of findings) {
    if (finding.workflow_name) {
      const existing = byWorkflow.get(finding.workflow_name);
      if (existing) existing.push(finding);
      else byWorkflow.set(finding.workflow_name, [finding]);
    } else {
      runLevel.push(finding);
    }
  }

  return { byWorkflow, runLevel };
}

/**
 * The run as a list of rows, oldest first.
 *
 * Each row carries the elapsed offset, a readable label, and whether the two
 * versions agreed there. `finding` is the first difference attributed to the
 * row (the row only needs to know that it diverged); `findingCount` is how many
 * landed on it, so a caller can tell whether it is showing all of them.
 */
export function buildSequence(events, findings = []) {
  if (!events || events.length === 0) return [];

  const shown = events.filter((event) => SHOWN_STAGES.has(event.stage));
  if (shown.length === 0) return [];

  const { byWorkflow, runLevel } = groupFindings(findings);
  // Run-level differences are shown once, on the last DB snapshot — the after
  // snapshot when there is one, so they read as the state the run left behind.
  const runLevelRow = shown.filter((e) => e.stage === "observing_postgres").pop();

  const start = events[0].timestamp;

  return shown.map((event) => {
    const engine = ENGINE_STAGES.has(event.stage);

    let matched = [];
    if (!engine) {
      if (event.stage === "workflow_completed") {
        matched = byWorkflow.get(workflowOf(event)) ?? [];
      } else if (event === runLevelRow) {
        matched = runLevel;
      }
    }

    const finding = matched[0] ?? null;
    return {
      timestamp: event.timestamp,
      elapsed: formatElapsed((event.timestamp - start) * 1000),
      stage: event.stage,
      label: label(event),
      // What the pipeline said at the time. The label is the short form a row
      // needs; a view with room for a sentence should show the sentence.
      message: event.message ?? null,
      // A diverged row shows the surface that actually differed, which is the
      // only way an outbound difference gets its own mark.
      icon: (finding && ICON[finding.category]) ?? STAGE_ICON[event.stage] ?? ICON.engine,
      status: engine ? "engine" : finding ? "diverge" : "match",
      finding,
      findingCount: matched.length,
    };
  });
}

/** How many findings no row could account for, so the view can say so. */
export function unattributedCount(rows, findings = []) {
  const attributed = rows.reduce((sum, row) => sum + row.findingCount, 0);
  return Math.max(0, findings.length - attributed);
}
