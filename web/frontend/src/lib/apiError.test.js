import { describe, expect, it } from "vitest";
import { ApiError, looksLikeApiJson } from "./apiError";

describe("ApiError", () => {
  it("is an Error, so nothing that catches Error misses it", () => {
    const err = new ApiError("boom");
    expect(err).toBeInstanceOf(Error);
    expect(err.message).toBe("boom");
  });

  it("defaults to a served error rather than an absent server", () => {
    const err = new ApiError("boom");
    expect(err.offline).toBe(false);
    expect(err.status).toBeNull();
  });

  it("carries the status and the offline flag when given them", () => {
    expect(new ApiError("nope", { status: 404 }).status).toBe(404);
    expect(new ApiError("nothing there", { offline: true }).offline).toBe(true);
  });
});

describe("looksLikeApiJson", () => {
  it("accepts the header FastAPI actually sends", () => {
    expect(looksLikeApiJson("application/json")).toBe(true);
    expect(looksLikeApiJson("application/json; charset=utf-8")).toBe(true);
  });

  it("rejects the index.html a static host serves for /api/*", () => {
    // The whole point: this arrives with status 200, so only the type gives
    // it away.
    expect(looksLikeApiJson("text/html; charset=utf-8")).toBe(false);
  });

  it("rejects a response with no content type at all", () => {
    expect(looksLikeApiJson(null)).toBe(false);
    expect(looksLikeApiJson(undefined)).toBe(false);
    expect(looksLikeApiJson("")).toBe(false);
  });
});
