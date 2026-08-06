import { ApiError, looksLikeApiJson } from "./lib/apiError";

/**
 * One JSON call to the run server, with "there is no run server" as a
 * first-class outcome.
 *
 * This bundle is also deployed as a static site with no backend behind it, so
 * every one of these can come back as a network failure or as the app's own
 * index.html. Both raise an ApiError with offline set, which the pages read to
 * offer the bundled walkthroughs instead of an error.
 */
async function apiJson(path, options) {
  let resp;
  try {
    resp = await fetch(path, options);
  } catch (cause) {
    throw new ApiError(`Could not reach the run server at ${path}`, { offline: true, cause });
  }

  if (!looksLikeApiJson(resp.headers.get("content-type"))) {
    throw new ApiError(`No run server is serving ${path}`, { offline: true, status: resp.status });
  }

  let body;
  try {
    body = await resp.json();
  } catch (cause) {
    throw new ApiError(`The response to ${path} was not readable JSON`, {
      offline: true,
      status: resp.status,
      cause,
    });
  }

  if (!resp.ok) {
    // FastAPI puts the reason in `detail` — surface it instead of the bare status.
    throw new ApiError(body?.detail || `Request to ${path} failed: ${resp.status}`, {
      status: resp.status,
    });
  }
  return body;
}

export function fetchRuns(limit = 50) {
  return apiJson(`/api/runs?limit=${limit}`);
}

export function fetchRun(runId) {
  return apiJson(`/api/runs/${runId}`);
}

export function fetchManifests() {
  return apiJson("/api/manifests");
}

export function triggerRun(manifestPath) {
  return apiJson("/api/runs/trigger", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ manifest_path: manifestPath }),
  });
}

/** WebSocket URL for a live run, on whatever host served this page. */
export function runStreamUrl(streamId) {
  const scheme = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${scheme}//${window.location.host}/api/runs/${streamId}/stream`;
}
