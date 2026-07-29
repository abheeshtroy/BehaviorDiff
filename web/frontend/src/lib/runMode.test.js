import { beforeEach, describe, expect, it } from "vitest";
import { noteRunFailure, realRunsAvailable, resetRunMode } from "./runMode";

beforeEach(() => resetRunMode());

describe("noteRunFailure", () => {
  it("recognises a dead Docker daemon from the error type", () => {
    expect(noteRunFailure({ errorType: "DockerException", message: "boom" })).toBe(true);
  });

  it("recognises the message the SDK actually produces when Docker is off", () => {
    // Verbatim from a real run with Docker Desktop closed.
    const message =
      "Error while fetching server API version: ('Connection aborted.', " +
      "FileNotFoundError(2, 'No such file or directory'))";
    expect(noteRunFailure({ errorType: "DockerException", message })).toBe(true);
  });

  it("does not blame Docker for an unrelated failure", () => {
    expect(noteRunFailure({ errorType: "ManifestError", message: "seed.sql not found" }))
      .toBe(false);
  });

  it("tolerates a failure with no detail at all", () => {
    expect(noteRunFailure()).toBe(false);
    expect(noteRunFailure({})).toBe(false);
  });
});

describe("realRunsAvailable", () => {
  it("is true before anything has failed", () => {
    expect(realRunsAvailable()).toBe(true);
  });

  it("latches off once the daemon is known to be down", () => {
    noteRunFailure({ errorType: "DockerException", message: "cannot connect" });
    expect(realRunsAvailable()).toBe(false);
  });

  it("stays on after a failure that is not the daemon's fault", () => {
    noteRunFailure({ errorType: "ManifestError", message: "bad yaml" });
    expect(realRunsAvailable()).toBe(true);
  });

  it("does not un-latch when a later failure is unrelated", () => {
    noteRunFailure({ errorType: "DockerException", message: "cannot connect" });
    noteRunFailure({ errorType: "ManifestError", message: "bad yaml" });
    expect(realRunsAvailable()).toBe(false);
  });
});
