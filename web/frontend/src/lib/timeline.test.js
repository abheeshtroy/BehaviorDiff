import { describe, expect, it } from "vitest";
import { buildTimeline, formatDuration, nearestEvent, positionForEvent } from "./timeline";
import { SCENARIOS } from "../demoData";

const T0 = 1_753_000_000;

const at = (offsetMs) => ({ timestamp: T0 + offsetMs / 1000 });

const event = (stage, offsetMs, data = null) => ({
  stage,
  message: stage,
  timestamp: T0 + offsetMs / 1000,
  data,
});

/** The stream a real run produces, start to finish. */
const run = () => [
  event("environments_starting", 0, { direct: false }),
  event("environments_ready", 1800, { base_url: "http://a", target_url: "http://b" }),
  event("observing_postgres", 2000, { phase: "before" }),
  event("workflow_started", 2040, { name: "checkout", index: 0, total: 1 }),
  event("workflow_completed", 2440, { name: "checkout", index: 0, total: 1 }),
  event("observing_http", 2600),
  event("observing_postgres", 2700, { phase: "after" }),
  event("comparing", 2800),
  event("done", 3000),
];

describe("formatDuration", () => {
  it("shows sub-second runs in whole milliseconds, with no sign", () => {
    expect(formatDuration(0)).toBe("0ms");
    expect(formatDuration(940.6)).toBe("941ms");
  });

  it("switches to seconds at a second", () => {
    expect(formatDuration(1000)).toBe("1.0s");
    expect(formatDuration(4340)).toBe("4.3s");
  });

  it("never renders a negative duration", () => {
    expect(formatDuration(-5)).toBe("0ms");
  });
});

describe("positionForEvent", () => {
  const first = at(0);
  const last = at(1000);

  it("puts the first event at the left edge and the last at the right", () => {
    expect(positionForEvent(first, first.timestamp, last.timestamp, 400)).toBe(0);
    expect(positionForEvent(last, first.timestamp, last.timestamp, 400)).toBe(400);
  });

  it("places an event in proportion to when it happened", () => {
    expect(positionForEvent(at(250), first.timestamp, last.timestamp, 400)).toBe(100);
    expect(positionForEvent(at(500), first.timestamp, last.timestamp, 400)).toBe(200);
  });

  it("reports percentages when asked for a bar 100 wide", () => {
    expect(positionForEvent(at(750), first.timestamp, last.timestamp, 100)).toBe(75);
  });

  it("keeps a single-event timeline at the left edge rather than dividing by zero", () => {
    expect(positionForEvent(first, first.timestamp, first.timestamp, 400)).toBe(0);
    expect(Number.isFinite(positionForEvent(first, first.timestamp, first.timestamp, 400))).toBe(true);
  });

  it("pins an out-of-range event to the nearer edge", () => {
    expect(positionForEvent(at(-500), first.timestamp, last.timestamp, 400)).toBe(0);
    expect(positionForEvent(at(1500), first.timestamp, last.timestamp, 400)).toBe(400);
  });
});

describe("nearestEvent", () => {
  const events = [at(0), at(500), at(1000)];
  const first = at(0).timestamp;
  const last = at(1000).timestamp;

  it("snaps to the closest marker", () => {
    expect(nearestEvent(0, events, first, last, 400)).toBe(0);
    expect(nearestEvent(30, events, first, last, 400)).toBe(0);
    expect(nearestEvent(170, events, first, last, 400)).toBe(1);
    expect(nearestEvent(210, events, first, last, 400)).toBe(1);
    expect(nearestEvent(380, events, first, last, 400)).toBe(2);
  });

  it("snaps to an end when the cursor runs past the bar", () => {
    expect(nearestEvent(-40, events, first, last, 400)).toBe(0);
    expect(nearestEvent(9999, events, first, last, 400)).toBe(2);
  });

  it("gives an exact tie to the earlier event", () => {
    expect(nearestEvent(100, events, first, last, 400)).toBe(0);
  });

  it("stays on the only event of a single-event timeline", () => {
    const one = [at(0)];
    expect(nearestEvent(0, one, first, first, 400)).toBe(0);
    expect(nearestEvent(400, one, first, first, 400)).toBe(0);
  });

  it("has nothing to snap to in an empty timeline", () => {
    expect(nearestEvent(120, [], first, last, 400)).toBe(-1);
    expect(nearestEvent(120, null, first, last, 400)).toBe(-1);
  });
});

describe("buildTimeline", () => {
  it("has no marks and no span for a run with no recorded events", () => {
    expect(buildTimeline(null)).toEqual({ marks: [], start: 0, end: 0, durationMs: 0 });
    expect(buildTimeline([])).toEqual({ marks: [], start: 0, end: 0, durationMs: 0 });
  });

  it("has no marks when the stream holds only infrastructure stages", () => {
    expect(buildTimeline([event("environments_starting", 0), event("persisting", 10)]).marks).toEqual([]);
  });

  it("spans the whole run, not just the steps worth a mark", () => {
    const { start, end, durationMs, marks } = buildTimeline(run(), []);

    // Standing the versions up is 2s of a 3s run; the bar keeps that time.
    expect(start).toBe(T0);
    expect(end).toBe(at(3000).timestamp);
    expect(durationMs).toBeCloseTo(3000, 6);
    expect(positionForEvent(marks[0], start, end, 100)).toBeCloseTo(66.67, 1);
  });

  it("marks the same steps the sequence view lists, in order", () => {
    const { marks } = buildTimeline(run(), []);

    expect(marks.map((m) => m.stage)).toEqual([
      "observing_postgres",
      "workflow_started",
      "workflow_completed",
      "observing_http",
      "observing_postgres",
      "comparing",
      "done",
    ]);
    expect(marks.every((m) => m.status === "match" || m.status === "engine")).toBe(true);
  });

  it("carries what the pipeline said, so the detail panel has a sentence to show", () => {
    const { marks } = buildTimeline(SCENARIOS["checkout-validation"].events, []);

    expect(marks[marks.length - 1].message).toBe("Run complete: 3 finding(s)");
    expect(marks.some((m) => m.message == null)).toBe(false);
  });

  it("marks the diverged step, and keeps the finding for the cursor to land on", () => {
    const scenario = SCENARIOS["checkout-validation"];
    // The scripted findings name their workflow under `workflow`; a stored run
    // uses workflow_name, which is what the timeline reads.
    const findings = scenario.findings.map((f) => ({ ...f, workflow_name: f.workflow }));
    const { marks } = buildTimeline(scenario.events, findings);

    const diverged = marks.filter((m) => m.status === "diverge");
    expect(diverged).toHaveLength(1);
    expect(diverged[0].label).toBe("Completed: checkout-flow");
    // The whole finding travels with the mark, so the panel has its evidence.
    expect(diverged[0].finding).toBe(findings[0]);
    expect(diverged[0].findingCount).toBe(3);
  });

  it("places every mark inside the bar, in the order the run produced them", () => {
    const scenario = SCENARIOS["checkout-validation"];
    const { marks, start, end } = buildTimeline(scenario.events, []);
    const positions = marks.map((m) => positionForEvent(m, start, end, 100));

    expect(positions.every((p) => p >= 0 && p <= 100)).toBe(true);
    expect(positions).toEqual([...positions].sort((a, b) => a - b));
    // The stream ends on `done`, so the last mark closes the bar.
    expect(positions[positions.length - 1]).toBe(100);
  });

  it("puts a single-mark run at the left edge", () => {
    const { marks, start, end } = buildTimeline([event("done", 0)], []);

    expect(marks).toHaveLength(1);
    expect(positionForEvent(marks[0], start, end, 100)).toBe(0);
    expect(nearestEvent(50, marks, start, end, 100)).toBe(0);
  });
});
