import { describe, expect, it } from "vitest";
import { buildSequence, formatElapsed, unattributedCount } from "./sequence";
import { SCENARIOS } from "../demoData";

const T0 = 1_753_000_000;

const event = (stage, offsetMs, data = null) => ({
  stage,
  message: stage,
  timestamp: T0 + offsetMs / 1000,
  data,
});

/** Two workflows, with the surrounding stream a real run produces. */
const twoWorkflowRun = () => [
  event("environments_starting", 0, { direct: false }),
  event("environments_ready", 1800, { base_url: "http://a", target_url: "http://b" }),
  event("observing_postgres", 2000, { phase: "before" }),
  event("workflow_started", 2040, { name: "checkout", index: 0, total: 2 }),
  event("workflow_completed", 2440, { name: "checkout", index: 0, total: 2 }),
  event("workflow_started", 2500, { name: "refund", index: 1, total: 2 }),
  event("workflow_completed", 3700, { name: "refund", index: 1, total: 2 }),
  event("workflows_complete", 3720, { count: 2 }),
  event("observing_http", 3800),
  event("observing_postgres", 3900, { phase: "after" }),
  event("comparing", 4000),
  event("classifying", 4200, { findings: 1 }),
  event("persisting", 4300),
  event("done", 4340),
];

const finding = (category, workflow_name = null) => ({
  category,
  workflow_name,
  step_index: 1,
  summary: `${category} differed`,
  severity: "changed",
  evidence_base: { status_code: 200 },
  evidence_target: { status_code: 400 },
});

describe("formatElapsed", () => {
  it("calls the start of the run zero, with no sign", () => {
    expect(formatElapsed(0)).toBe("0ms");
  });

  it("shows sub-second offsets in whole milliseconds", () => {
    expect(formatElapsed(40)).toBe("+40ms");
    expect(formatElapsed(999.4)).toBe("+999ms");
  });

  it("switches to seconds at a second", () => {
    expect(formatElapsed(1000)).toBe("+1.0s");
    expect(formatElapsed(1240)).toBe("+1.2s");
    expect(formatElapsed(12_500)).toBe("+12.5s");
  });

  it("never renders a negative offset", () => {
    expect(formatElapsed(-5)).toBe("0ms");
  });
});

describe("buildSequence", () => {
  it("returns nothing for a run with no recorded events", () => {
    expect(buildSequence(null)).toEqual([]);
    expect(buildSequence(undefined)).toEqual([]);
    expect(buildSequence([])).toEqual([]);
    expect(buildSequence([], [finding("http", "checkout")])).toEqual([]);
  });

  it("returns nothing when the stream holds only infrastructure stages", () => {
    expect(buildSequence([event("environments_starting", 0), event("persisting", 10)])).toEqual([]);
  });

  it("keeps the stages worth showing, in order, and drops the rest", () => {
    const rows = buildSequence(twoWorkflowRun(), []);

    expect(rows.map((r) => r.stage)).toEqual([
      "observing_postgres",
      "workflow_started",
      "workflow_completed",
      "workflow_started",
      "workflow_completed",
      "observing_http",
      "observing_postgres",
      "comparing",
      "classifying",
      "done",
    ]);
    // Environment setup, workflows_complete and persisting are harness noise.
    expect(rows.some((r) => r.stage === "environments_ready")).toBe(false);
    expect(rows.some((r) => r.stage === "persisting")).toBe(false);
  });

  it("measures elapsed time from the first event, not the first shown row", () => {
    const rows = buildSequence(twoWorkflowRun(), []);

    // The run started 2s before the first row that survives the filter.
    expect(rows[0].elapsed).toBe("+2.0s");
    expect(rows[rows.length - 1].elapsed).toBe("+4.3s");
    expect(rows.map((r) => r.timestamp)).toEqual([...rows.map((r) => r.timestamp)].sort((a, b) => a - b));
  });

  it("calls the first row 0ms when the stream starts with a shown stage", () => {
    const rows = buildSequence(
      [event("workflow_started", 0, { name: "checkout" }), event("workflow_completed", 40, { name: "checkout" })],
      [],
    );

    expect(rows.map((r) => r.elapsed)).toEqual(["0ms", "+40ms"]);
  });

  it("labels each kind of row readably", () => {
    const rows = buildSequence(twoWorkflowRun(), []);
    const labelFor = (stage) => rows.find((r) => r.stage === stage).label;

    expect(rows[1].label).toBe("checkout");
    expect(rows[2].label).toBe("Completed: checkout");
    expect(rows[0].label).toBe("DB snapshot (before)");
    expect(labelFor("observing_http")).toBe("HTTP comparison");
    expect(labelFor("comparing")).toBe("Comparing observations");
    expect(labelFor("classifying")).toBe("Classifying findings");
    expect(labelFor("done")).toBe("Run complete");
  });

  it("attaches a workflow's finding to that workflow's completion", () => {
    const refundFinding = finding("http", "refund");
    const rows = buildSequence(twoWorkflowRun(), [refundFinding]);

    const diverged = rows.filter((r) => r.status === "diverge");
    expect(diverged).toHaveLength(1);
    expect(diverged[0].label).toBe("Completed: refund");
    expect(diverged[0].finding).toBe(refundFinding);
    expect(diverged[0].findingCount).toBe(1);

    // The workflow that agreed is not implicated by its neighbour.
    expect(rows.find((r) => r.label === "Completed: checkout").status).toBe("match");
    expect(rows.find((r) => r.label === "Completed: checkout").finding).toBeNull();
  });

  it("keeps the first of several findings on one workflow, and counts them all", () => {
    const first = finding("http", "checkout");
    const rows = buildSequence(twoWorkflowRun(), [first, finding("http", "checkout")]);
    const row = rows.find((r) => r.label === "Completed: checkout");

    expect(row.finding).toBe(first);
    expect(row.findingCount).toBe(2);
  });

  it("lands findings with no workflow on the closing DB snapshot", () => {
    const rows = buildSequence(twoWorkflowRun(), [finding("postgres"), finding("postgres")]);
    const snapshots = rows.filter((r) => r.stage === "observing_postgres");

    // Before the workflows ran, nothing had diverged yet.
    expect(snapshots[0].status).toBe("match");
    expect(snapshots[1].status).toBe("diverge");
    expect(snapshots[1].findingCount).toBe(2);
  });

  it("marks every non-engine row as matching when nothing was found", () => {
    const rows = buildSequence(twoWorkflowRun(), []);

    for (const row of rows) {
      expect(row.status).toBe(row.stage === "comparing" || row.stage === "classifying" || row.stage === "done" ? "engine" : "match");
      expect(row.finding).toBeNull();
      expect(row.findingCount).toBe(0);
    }
  });

  it("leaves the engine's own stages neutral even when the run diverged", () => {
    const rows = buildSequence(twoWorkflowRun(), [finding("http", "checkout"), finding("postgres")]);

    for (const stage of ["comparing", "classifying", "done"]) {
      const row = rows.find((r) => r.stage === stage);
      expect(row.status).toBe("engine");
      expect(row.finding).toBeNull();
    }
  });

  it("shows the surface that diverged, so an outbound difference is not read as HTTP", () => {
    const rows = buildSequence(twoWorkflowRun(), [finding("outbound", "checkout")]);

    expect(rows.find((r) => r.label === "Completed: checkout").icon).toBe("⟐");
    expect(rows.find((r) => r.label === "Completed: refund").icon).toBe("→");
    expect(rows.find((r) => r.stage === "observing_postgres").icon).toBe("◆");
    expect(rows.find((r) => r.stage === "done").icon).toBe("⊙");
  });
});

describe("unattributedCount", () => {
  it("is zero when every finding found a row", () => {
    const findings = [finding("http", "checkout"), finding("postgres")];
    expect(unattributedCount(buildSequence(twoWorkflowRun(), findings), findings)).toBe(0);
  });

  it("counts findings whose workflow never appears in the stream", () => {
    const findings = [finding("http", "a-workflow-that-did-not-run")];
    expect(unattributedCount(buildSequence(twoWorkflowRun(), findings), findings)).toBe(1);
  });

  it("counts run-level findings when the stream has no DB snapshot to hang them on", () => {
    const events = [
      event("workflow_started", 0, { name: "checkout" }),
      event("workflow_completed", 40, { name: "checkout" }),
      event("done", 80),
    ];
    const findings = [finding("postgres"), finding("outbound")];

    expect(unattributedCount(buildSequence(events, findings), findings)).toBe(2);
  });
});

describe("the scripted scenarios", () => {
  it.each(Object.values(SCENARIOS).map((s) => [s.id, s]))("%s builds a sequence", (_id, scenario) => {
    const rows = buildSequence(scenario.events, []);

    expect(rows.length).toBeGreaterThan(0);
    // Standing the two versions up dominates the scripted stream, so the first
    // row that survives the filter is already two seconds in.
    expect(rows[0].elapsed).toBe("+2.0s");
    expect(rows[rows.length - 1].label).toBe("Run complete");
  });

  it("marks the scripted workflow as diverged from the scripted findings", () => {
    const scenario = SCENARIOS["checkout-validation"];
    // The scripted findings name their workflow under `workflow`; a stored run
    // uses workflow_name, which is what this reads.
    const findings = scenario.findings.map((f) => ({ ...f, workflow_name: f.workflow }));
    const rows = buildSequence(scenario.events, findings);

    const diverged = rows.filter((r) => r.status === "diverge");
    expect(diverged).toHaveLength(1);
    expect(diverged[0].label).toBe("Completed: checkout-flow");
    expect(diverged[0].findingCount).toBe(3);
  });
});
