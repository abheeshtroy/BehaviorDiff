import { describe, expect, it } from "vitest";
import { hasDiverged } from "./stream";

const event = (stage, data = null) => ({ stage, message: stage, timestamp: 0, data });

const classified = (...labels) => ({
  classification: {
    classifications: labels.map((classification, finding_index) => ({
      finding_index,
      classification,
    })),
  },
});

describe("hasDiverged", () => {
  it("is false before anything has happened", () => {
    expect(hasDiverged([])).toBe(false);
  });

  it("is false while the run is merely in progress", () => {
    expect(hasDiverged([event("environments_starting"), event("comparing")])).toBe(false);
  });

  it("is true as soon as the run errors", () => {
    expect(hasDiverged([event("environments_starting"), event("error")])).toBe(true);
  });

  it("is true for a finished run carrying a suspicious finding", () => {
    expect(hasDiverged([event("done", classified("intended", "suspicious"))])).toBe(true);
  });

  it("is false for a finished run whose findings are all benign", () => {
    expect(hasDiverged([event("done", classified("intended", "noise"))])).toBe(false);
  });

  it("is false for a web-triggered run, which is never classified", () => {
    // The AI layer is gated on diff_text, which the web trigger never sets,
    // so classification is null on every run started from the browser. A
    // finished run must not light up merely for finishing.
    expect(hasDiverged([event("done", { run_id: 7, classification: null })])).toBe(false);
  });

  it("is false when the done frame carries no data at all", () => {
    expect(hasDiverged([event("done")])).toBe(false);
  });

  it("stays true once tripped, whatever arrives afterwards", () => {
    expect(hasDiverged([event("error"), event("done", classified("intended"))])).toBe(true);
  });
});
