import { describe, expect, it } from "vitest";
import { scenarioForManifest, watchLine } from "./scenarios";
import { SCENARIOS } from "../demoData";

describe("scenarioForManifest", () => {
  it("matches a bare filename", () => {
    expect(scenarioForManifest("scenario1-checkout-validation.yaml")?.id)
      .toBe("checkout-validation");
  });

  it("matches the relative path /api/manifests returns", () => {
    expect(scenarioForManifest("demo/manifests/scenario2-retry-logic.yaml")?.id)
      .toBe("retry-logic");
  });

  it("matches an absolute path, as stored on a run", () => {
    expect(scenarioForManifest("/srv/app/demo/manifests/scenario3-response-cleanup.yaml")?.id)
      .toBe("api-cleanup");
  });

  it("returns null for an unknown manifest rather than guessing", () => {
    expect(scenarioForManifest("demo/manifests/some-other-app.yaml")).toBeNull();
  });

  it.each([null, undefined, ""])("returns null for %p", (input) => {
    expect(scenarioForManifest(input)).toBeNull();
  });

  it("covers every scenario — a scenario with no manifest is unreachable", () => {
    for (const scenario of Object.values(SCENARIOS)) {
      expect(scenario.manifest, `${scenario.id} has no manifest`).toBeTruthy();
      expect(scenarioForManifest(scenario.manifest)?.id).toBe(scenario.id);
    }
  });

  it("does not derive the filename from the id", () => {
    // api-cleanup lives in scenario3-response-cleanup.yaml. Any string-munging
    // shortcut would break exactly here.
    expect(SCENARIOS["api-cleanup"].manifest).toBe("scenario3-response-cleanup.yaml");
  });
});

describe("watchLine", () => {
  it("names the surfaces without naming the culprit", () => {
    const line = watchLine(SCENARIOS["checkout-validation"]);
    expect(line).toBe("3 differences across HTTP responses, database rows and outbound calls");
  });

  it("stays singular for a single finding", () => {
    expect(watchLine(SCENARIOS["api-cleanup"])).toBe("1 difference across HTTP responses");
  });

  it("joins two surfaces with 'and', no comma", () => {
    expect(watchLine(SCENARIOS["retry-logic"]))
      .toBe("2 differences across database rows and outbound calls");
  });

  it("gives away nothing about which finding is suspicious", () => {
    for (const scenario of Object.values(SCENARIOS)) {
      expect(watchLine(scenario)).not.toMatch(/suspicious|intended|bug/i);
    }
  });

  it("returns null for no scenario", () => {
    expect(watchLine(null)).toBeNull();
  });

  it("returns null when a scenario has no findings", () => {
    expect(watchLine({ findings: [] })).toBeNull();
  });
});
