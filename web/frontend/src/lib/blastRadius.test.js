import { describe, expect, it } from "vitest";
import { BASE_COLUMNS, buildGrid, columnFor, RUN_LEVEL } from "./blastRadius";

const finding = (category, workflow_name = null) => ({
  category,
  workflow_name,
  summary: `${category} changed`,
  severity: "changed",
});

describe("columnFor", () => {
  it.each([
    ["http", "HTTP"],
    ["http_status", "HTTP"],
    ["http_body", "HTTP"],
    ["http_headers", "HTTP"],
    ["postgres", "Postgres"],
    ["outbound", "Outbound"],
  ])("maps %s to %s", (category, column) => {
    expect(columnFor(finding(category))).toBe(column);
  });

  it("keeps an unmapped category as its own column rather than hiding it", () => {
    expect(columnFor(finding("latency"))).toBe("latency");
  });
});

describe("buildGrid", () => {
  it("shows the three observed surfaces even when nothing landed on them", () => {
    const grid = buildGrid([finding("http", "checkout")]);
    expect(grid.columns).toEqual(BASE_COLUMNS);
    expect(grid.countAt("checkout", "Postgres")).toBe(0);
    expect(grid.countAt("checkout", "Outbound")).toBe(0);
  });

  it("is empty for no findings, with no rows to plot", () => {
    const grid = buildGrid([]);
    expect(grid.rows).toEqual([]);
    expect(grid.cellCount).toBe(0);
    expect(grid.affectedWorkflows).toBe(0);
  });

  it("counts findings per workflow and surface", () => {
    const grid = buildGrid([
      finding("http", "checkout"),
      finding("http", "checkout"),
      finding("http", "refund"),
      finding("outbound", "refund"),
    ]);

    expect(grid.rows).toEqual(["checkout", "refund"]);
    expect(grid.countAt("checkout", "HTTP")).toBe(2);
    expect(grid.countAt("refund", "HTTP")).toBe(1);
    expect(grid.countAt("refund", "Outbound")).toBe(1);
    expect(grid.columnTotal("HTTP")).toBe(3);
    expect(grid.columnTotal("Postgres")).toBe(0);
  });

  it("collects findings with no workflow in one run-level row, last", () => {
    const grid = buildGrid([
      finding("postgres"),
      finding("http", "checkout"),
      finding("outbound"),
    ]);

    expect(grid.rows).toEqual(["checkout", RUN_LEVEL]);
    expect(grid.countAt(RUN_LEVEL, "Postgres")).toBe(1);
    expect(grid.countAt(RUN_LEVEL, "Outbound")).toBe(1);
    // Not attributed to the one workflow that happened to be named.
    expect(grid.countAt("checkout", "Postgres")).toBe(0);
  });

  it("appends an unrecognised category as its own column, in the order first seen", () => {
    const grid = buildGrid([
      finding("latency", "checkout"),
      finding("http", "checkout"),
      finding("something-new", "checkout"),
      finding("latency", "checkout"),
    ]);

    expect(grid.columns).toEqual([...BASE_COLUMNS, "latency", "something-new"]);
    expect(grid.countAt("checkout", "latency")).toBe(2);
  });

  it("counts the cells that came out identical", () => {
    const grid = buildGrid([finding("http", "checkout"), finding("postgres", "checkout")]);

    // One row, three columns; two carried a difference.
    expect(grid.cellCount).toBe(3);
    expect(grid.cleanCells).toBe(1);
  });

  it("does not count the run-level row as an affected workflow", () => {
    const grid = buildGrid([finding("http", "checkout"), finding("postgres")]);
    expect(grid.rows).toHaveLength(2);
    expect(grid.affectedWorkflows).toBe(1);
  });
});
