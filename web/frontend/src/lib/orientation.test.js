import { describe, expect, it } from "vitest";
import { beatFor, GENERIC_BEAT } from "./orientation";
import { SCENARIOS } from "../demoData";

describe("beatFor", () => {
  it("heads the card with the scenario's own pull request", () => {
    expect(beatFor("retry-logic")).toMatchObject({
      pr: "#495",
      title: "Refactor background job retry logic",
      scale: "4 lines changed",
    });
  });

  it("accepts a scenario object as well as an id", () => {
    expect(beatFor(SCENARIOS["api-cleanup"])).toEqual(beatFor("api-cleanup"));
  });

  it("counts only added and removed lines as the scale", () => {
    // api-cleanup's diff is two context lines, one removal, one addition.
    expect(beatFor("api-cleanup").scale).toBe("2 lines changed");
  });

  it("appends the approval count to the CI checks", () => {
    expect(beatFor("checkout-validation").checks)
      .toEqual(["47 tests passing", "94% coverage", "lint clean", "2 approvals"]);
  });

  it("keeps the approval count singular at one", () => {
    expect(beatFor("retry-logic").checks).toContain("1 approval");
  });

  it("tags the intended change apart from the rest", () => {
    expect(beatFor("checkout-validation").changes).toEqual([
      { tag: "intended", tone: "intended", text: "POST /checkout: status 200 → 400" },
      { tag: "bug", tone: "bug", text: "Discount silently cleared" },
      { tag: "bug", tone: "bug", text: "Card charged on a rejected order" },
    ]);
  });

  it("tags every change a bug when the scenario has no intended finding", () => {
    expect(beatFor("retry-logic").changes.map((c) => c.tag)).toEqual(["bug", "bug"]);
  });

  it("gives every scenario a distinct beat — no scenario borrows another's", () => {
    const beats = Object.keys(SCENARIOS).map((id) => beatFor(id));
    for (const field of ["pr", "title", "headline", "sub", "caption"]) {
      const seen = beats.map((b) => b[field]);
      expect(seen.every(Boolean), `a scenario is missing ${field}`).toBe(true);
      expect(new Set(seen).size, `${field} is shared between scenarios`).toBe(beats.length);
    }
  });

  it("draws the change list from the scenario's own findings", () => {
    for (const scenario of Object.values(SCENARIOS)) {
      expect(beatFor(scenario.id).changes.map((c) => c.text))
        .toEqual(scenario.findings.map((f) => f.summary));
    }
  });

  it("returns null for an unknown scenario rather than falling back to one", () => {
    expect(beatFor("no-such-scenario")).toBeNull();
    expect(beatFor(null)).toBeNull();
  });
});

describe("GENERIC_BEAT", () => {
  it("makes the argument without claiming a pull request it has no data for", () => {
    expect(GENERIC_BEAT.headline).toBeTruthy();
    expect(GENERIC_BEAT.sub).toBeTruthy();
    expect(GENERIC_BEAT.pr).toBeUndefined();
    expect(GENERIC_BEAT.checks).toBeUndefined();
    expect(GENERIC_BEAT.changes).toBeUndefined();
  });
});
