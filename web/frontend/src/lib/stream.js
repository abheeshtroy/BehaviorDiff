/**
 * Whether a run has hit something worth colouring the background for.
 *
 * The split-colour state means "something bad was found", not "the run
 * ended" — a clean run stays evenly lit. A real run has no scripted
 * divergence point like the demo does, so the only two things that earn it
 * are a failure and a finished run carrying suspicious findings. Events only
 * ever accumulate, so deriving this per render is already latching.
 */
export function hasDiverged(events) {
  return events.some((event) => {
    if (event.stage === "error") return true;
    if (event.stage !== "done") return false;
    // Absent when the AI layer was skipped, which is every web-triggered run —
    // nothing is labelled suspicious and the background rests.
    const classified = event.data?.classification?.classifications ?? [];
    return classified.some((c) => c.classification === "suspicious");
  });
}
