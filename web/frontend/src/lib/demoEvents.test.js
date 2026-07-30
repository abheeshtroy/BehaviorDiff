import { describe, expect, it } from "vitest";
import { SCENARIOS, SCENARIO_LIST } from "../demoData";

/**
 * The scripted scenarios carry a synthetic event stream so the views built on a
 * run's progress work in the demo too. What matters is that it is shaped like a
 * real one — same stages, same order, same data keys — since a view must not
 * have to know which kind of run it is looking at.
 */
describe("scripted event streams", () => {
  it.each(SCENARIO_LIST.map((s) => [s.id, s]))("%s emits a plausible run", (_id, scenario) => {
    const stages = scenario.events.map((e) => e.stage);

    expect(stages[0]).toBe("environments_starting");
    expect(stages[stages.length - 1]).toBe("done");
    expect(stages).toContain("environments_ready");
    expect(stages).toContain("observing_http");
    expect(stages).toContain("comparing");
    expect(stages).toContain("persisting");
    // Persisting is the last thing before the terminal event, as in the pipeline.
    expect(stages[stages.length - 2]).toBe("persisting");
  });

  it.each(SCENARIO_LIST.map((s) => [s.id, s]))("%s advances monotonically in time", (_id, scenario) => {
    const timestamps = scenario.events.map((e) => e.timestamp);
    expect(timestamps).toEqual([...timestamps].sort((a, b) => a - b));
    // Distinct instants: two events at the same moment would collapse on a timeline.
    expect(new Set(timestamps).size).toBe(timestamps.length);
  });

  it.each(SCENARIO_LIST.map((s) => [s.id, s]))("%s pairs every workflow start with a finish", (_id, scenario) => {
    const workflowStages = scenario.events
      .map((e) => e.stage)
      .filter((s) => s.startsWith("workflow_") && s !== "workflows_complete");

    expect(workflowStages.length).toBeGreaterThan(0);
    expect(workflowStages.length % 2).toBe(0);
    for (let i = 0; i < workflowStages.length; i += 2) {
      expect(workflowStages[i]).toBe("workflow_started");
      expect(workflowStages[i + 1]).toBe("workflow_completed");
    }
  });

  it("names workflows the way the pipeline does, from the scenario's own findings", () => {
    const scenario = SCENARIOS["checkout-validation"];
    const started = scenario.events.filter((e) => e.stage === "workflow_started");

    expect(started.map((e) => e.data.name)).toEqual(["checkout-flow"]);
    expect(started[0].data).toEqual({ name: "checkout-flow", index: 0, total: 1 });
  });

  it("snapshots the database before and after the workflows run", () => {
    for (const scenario of SCENARIO_LIST) {
      const phases = scenario.events
        .filter((e) => e.stage === "observing_postgres")
        .map((e) => e.data.phase);
      expect(phases).toEqual(["before", "after"]);
    }
  });

  it("leaves the scripted findings untouched", () => {
    // The events are derived from the scenarios, not a replacement for them.
    expect(SCENARIOS["checkout-validation"].findings).toHaveLength(3);
    expect(SCENARIOS["checkout-validation"].manifest).toBe("scenario1-checkout-validation.yaml");
  });
});
