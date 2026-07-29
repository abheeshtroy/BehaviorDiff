/**
 * Grouping raw pipeline stages into the four phases a viewer can follow.
 *
 * The pipeline emits stage names meant for a log — `environments_starting`,
 * `observing_postgres` — and it grows new ones over time. Rather than
 * enumerate them and go stale, unknown stages attach to whichever phase is
 * already running, so a new stage can never push the display into a phase
 * that hasn't been reached. The raw log stays available underneath; this is
 * a summary of it, not a replacement for it.
 */
export const PHASES = [
  { id: "build", label: "Building both versions" },
  { id: "run", label: "Running the workflow" },
  { id: "compare", label: "Comparing everything" },
  { id: "classify", label: "Classifying findings" },
];

// Stage names as emitted by engine/pipeline.py.
const STAGE_PHASE = {
  environments_starting: 0,
  environments_ready: 0,
  workflow_started: 1,
  workflow_completed: 1,
  workflows_complete: 1,
  observing_http: 2,
  observing_postgres: 2,
  postgres_observation_skipped: 2,
  comparing: 2,
  classifying: 3,
  // Saving is the tail of the run and has no phase of its own; it belongs
  // with the last one rather than implying a fifth step.
  persisting: 3,
};

/** Stages that end the run rather than belonging to a phase. */
export const TERMINAL_STAGES = new Set(["done", "error"]);

/**
 * Fold a list of run events into one status per phase.
 *
 * Returns an entry per PHASES item: "pending" (not reached), "active" (the
 * furthest phase reached, while the run is still going), "done" (a later
 * phase started, or the run finished), or "failed" for the phase that was
 * running when an error arrived.
 */
export function phaseStates(events, { live = true } = {}) {
  let furthest = -1;
  let failed = false;

  for (const event of events) {
    if (event.stage === "error") {
      failed = true;
      continue;
    }
    if (event.stage === "done") continue;

    const index = STAGE_PHASE[event.stage];
    // Unknown stage: it belongs to whatever is already running.
    if (index == null) continue;
    if (index > furthest) furthest = index;
  }

  const finished = events.some((e) => e.stage === "done");

  return PHASES.map((phase, i) => {
    let state = "pending";
    if (finished) state = i <= furthest ? "done" : "pending";
    else if (failed && i === furthest) state = "failed";
    else if (i < furthest) state = "done";
    else if (i === furthest) state = live && !failed ? "active" : "done";
    return { ...phase, state };
  });
}

/** True once any event has landed in a known phase. */
export function hasPhaseProgress(events) {
  return events.some((e) => STAGE_PHASE[e.stage] != null);
}
