/**
 * Whether this build can run real comparisons.
 *
 * A real run builds and starts two Docker environments on the server. The
 * public deployment has no Docker daemon, so `VITE_REAL_RUNS=off` at build
 * time turns the affordance off entirely rather than letting every visitor
 * discover it by waiting for a run to fail. The scripted replay is the
 * always-available path and stays first-class either way.
 */
export const REAL_RUNS_BUILD_ENABLED = import.meta.env.VITE_REAL_RUNS !== "off";

// Set once a run fails for a reason that will fail identically next time —
// a missing Docker daemon. Covers local dev with Docker Desktop closed,
// which the build flag can't know about.
let daemonKnownDown = false;

const DAEMON_DOWN_PATTERNS = [
  /docker/i,
  /cannot connect to the docker daemon/i,
  /connection aborted/i,
];

/**
 * Record a terminal run failure. Returns true if it looks like the Docker
 * daemon is unavailable, in which case later renders offer the scripted
 * path instead of inviting another doomed run.
 */
export function noteRunFailure({ errorType, message } = {}) {
  const text = `${errorType ?? ""} ${message ?? ""}`;
  const isDaemonDown = DAEMON_DOWN_PATTERNS.some((re) => re.test(text));
  if (isDaemonDown) daemonKnownDown = true;
  return isDaemonDown;
}

export function realRunsAvailable() {
  return REAL_RUNS_BUILD_ENABLED && !daemonKnownDown;
}

/** Test seam — the module-level latch would otherwise leak between cases. */
export function resetRunMode() {
  daemonKnownDown = false;
}
