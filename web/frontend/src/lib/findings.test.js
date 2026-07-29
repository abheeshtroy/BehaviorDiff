import { describe, expect, it } from "vitest";
import { changeIntent, findingKey, humanizeFinding, runHeadline } from "./findings";

const MANIFEST = "demo/manifests/scenario1-checkout-validation.yaml";

describe("findingKey", () => {
  it("keys a postgres row change by its table", () => {
    expect(findingKey({ category: "postgres", severity: "added", summary: "row inserted into payment_calls" }))
      .toBe("postgres:added:payment_calls");
    expect(findingKey({ category: "postgres", severity: "removed", summary: "row deleted from carts" }))
      .toBe("postgres:removed:carts");
    expect(findingKey({ category: "postgres", severity: "changed", summary: "row modified in carts (pk=1)" }))
      .toBe("postgres:changed:carts");
  });

  it("keys an http difference by method and path", () => {
    const finding = {
      category: "http",
      severity: "changed",
      summary: "POST /api/checkout: status 500 -> 400; body changed",
    };
    expect(findingKey(finding)).toBe("http:changed:POST /api/checkout");
  });

  it("keys an outbound call by method and path", () => {
    const finding = {
      category: "outbound",
      severity: "added",
      summary: "outbound call POST /charge made only by target",
    };
    expect(findingKey(finding)).toBe("outbound:added:POST /charge");
  });

  it("degrades to category and severity when there is no subject to parse", () => {
    expect(findingKey({ category: "latency", severity: "changed", summary: "p95 rose" }))
      .toBe("latency:changed");
    expect(findingKey(null)).toBe("");
  });
});

describe("humanizeFinding", () => {
  // These four are the exact summaries a real run of scenario 1 produces.
  it("rewrites the payment_calls insert a real run actually emits", () => {
    const finding = { category: "postgres", severity: "added", summary: "row inserted into payment_calls" };
    expect(humanizeFinding(finding, MANIFEST)).toBe("Card charged on a rejected order");
  });

  it("distinguishes the two carts rows by severity", () => {
    const added = { category: "postgres", severity: "added", summary: "row inserted into carts" };
    const removed = { category: "postgres", severity: "removed", summary: "row deleted from carts" };
    expect(humanizeFinding(added, MANIFEST)).toBe("Cart stored with its discount cleared");
    expect(humanizeFinding(removed, MANIFEST)).toBe("The discounted cart no longer exists");
  });

  it("rewrites the real checkout response difference", () => {
    const finding = {
      category: "http",
      severity: "changed",
      summary: "POST /api/checkout: status 500 -> 400; body changed; headers changed: content-length",
    };
    expect(humanizeFinding(finding, MANIFEST))
      .toBe("Checkout rejects the bad address with a 400 instead of failing with a 500");
  });

  it("rewrites a raw postgres summary using the scripted scenario", () => {
    const finding = { category: "postgres", severity: "changed", summary: "row modified in carts (pk=1)" };
    expect(humanizeFinding(finding, MANIFEST)).toBe("Discount silently cleared");
  });

  it("rewrites an outbound call the target made on its own", () => {
    const finding = {
      category: "outbound",
      severity: "added",
      summary: "outbound call POST /charge made only by target",
    };
    expect(humanizeFinding(finding, MANIFEST)).toBe("Card charged on a rejected order");
  });

  it("matches on an absolute manifest path, not just the relative one", () => {
    const finding = { category: "postgres", severity: "changed", summary: "row modified in carts (pk=1)" };
    const absolute = "/srv/behaviordiff/demo/manifests/scenario1-checkout-validation.yaml";
    expect(humanizeFinding(finding, absolute)).toBe("Discount silently cleared");
  });

  it("returns null for a manifest with no scripted scenario", () => {
    const finding = { category: "postgres", severity: "added", summary: "row inserted into orders" };
    expect(humanizeFinding(finding, "demo/manifests/some-other-app.yaml")).toBeNull();
  });

  it("returns null when the scenario has no finding of that shape", () => {
    const finding = { category: "latency", severity: "changed", summary: "p95 rose by 280ms" };
    expect(humanizeFinding(finding, MANIFEST)).toBeNull();
  });

  it("returns null rather than guessing when the raw summary already matches", () => {
    const finding = {
      category: "postgres",
      severity: "changed",
      summary: "Discount silently cleared",
    };
    expect(humanizeFinding(finding, MANIFEST)).toBeNull();
  });

  it("survives a missing finding or manifest", () => {
    expect(humanizeFinding(null, MANIFEST)).toBeNull();
    expect(humanizeFinding({ category: "http", severity: "changed" }, null)).toBeNull();
  });
});

describe("changeIntent", () => {
  it("prefers the intent the run itself recorded", () => {
    const run = {
      manifest_path: MANIFEST,
      intent: { summary: "From the real diff", expected_behavior_changes: ["a"] },
    };
    expect(changeIntent(run)).toEqual({
      summary: "From the real diff",
      expected: ["a"],
      source: "run",
    });
  });

  it("falls back to the scripted scenario when the run has no intent", () => {
    const intent = changeIntent({ manifest_path: MANIFEST });
    expect(intent.source).toBe("scenario");
    expect(intent.summary).toMatch(/empty carts/i);
    expect(intent.expected.length).toBeGreaterThan(0);
  });

  it("returns null when there is neither", () => {
    expect(changeIntent({ manifest_path: "demo/manifests/unknown.yaml" })).toBeNull();
    expect(changeIntent(null)).toBeNull();
  });
});

describe("runHeadline", () => {
  it("uses the scenario's PR and title for a demo manifest", () => {
    expect(runHeadline({ manifest_path: MANIFEST, id: "run_1", app_name: "shop-api" })).toEqual({
      badge: "#482",
      title: "Fix checkout validation for empty carts",
    });
  });

  it("falls back to the run id and app name otherwise", () => {
    const run = { manifest_path: "demo/manifests/other.yaml", id: "run_9", app_name: "other-api" };
    expect(runHeadline(run)).toEqual({ badge: "run_9", title: "other-api" });
  });
});
