import { describe, expect, it } from "vitest";
import { PHASES, hasPhaseProgress, phaseStates } from "./phases";

const ev = (stage) => ({ stage, message: stage, timestamp: 0, data: null });

const statesOf = (events, opts) => phaseStates(events, opts).map((p) => p.state);

describe("phaseStates", () => {
  it("reports every phase pending before anything arrives", () => {
    expect(statesOf([])).toEqual(["pending", "pending", "pending", "pending"]);
  });

  it("marks the furthest reached phase active while the run is live", () => {
    expect(statesOf([ev("environments_starting")])).toEqual([
      "active", "pending", "pending", "pending",
    ]);
  });

  it("closes earlier phases once a later one starts", () => {
    const events = [ev("environments_starting"), ev("environments_ready"), ev("workflow_started")];
    expect(statesOf(events)).toEqual(["done", "active", "pending", "pending"]);
  });

  it("groups every observing stage into the single compare phase", () => {
    const events = [ev("observing_http"), ev("observing_postgres"), ev("comparing")];
    // Reaching compare implies build and run finished, even though this
    // stream never showed them — the pipeline cannot observe before it runs.
    expect(statesOf(events)).toEqual(["done", "done", "active", "pending"]);
  });

  it("keeps persisting inside the classify phase rather than inventing a fifth", () => {
    expect(phaseStates([ev("persisting")])).toHaveLength(PHASES.length);
    expect(statesOf([ev("persisting")])).toEqual(["done", "done", "done", "active"]);
  });

  it("marks reached phases done when the run finishes", () => {
    const events = [ev("environments_starting"), ev("classifying"), ev("done")];
    expect(statesOf(events)).toEqual(["done", "done", "done", "done"]);
  });

  it("leaves phases the run never reached pending when it finishes early", () => {
    expect(statesOf([ev("environments_ready"), ev("done")])).toEqual([
      "done", "pending", "pending", "pending",
    ]);
  });

  it("fails the phase that was running when the error arrived", () => {
    const events = [ev("environments_starting"), ev("workflow_started"), ev("error")];
    expect(statesOf(events)).toEqual(["done", "failed", "pending", "pending"]);
  });

  it("does not advance the display on a stage it has never seen before", () => {
    const events = [ev("workflow_started"), ev("some_future_stage")];
    expect(statesOf(events)).toEqual(["done", "active", "pending", "pending"]);
  });

  it("settles the active phase to done once the stream is no longer live", () => {
    expect(statesOf([ev("workflow_started")], { live: false })).toEqual([
      "done", "done", "pending", "pending",
    ]);
  });
});

describe("hasPhaseProgress", () => {
  it("is false for an empty or unrecognised stream", () => {
    expect(hasPhaseProgress([])).toBe(false);
    expect(hasPhaseProgress([ev("some_future_stage")])).toBe(false);
  });

  it("is true once a known stage lands", () => {
    expect(hasPhaseProgress([ev("comparing")])).toBe(true);
  });
});
