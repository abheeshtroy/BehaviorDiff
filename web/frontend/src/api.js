export async function fetchRuns(limit = 50) {
  const resp = await fetch(`/api/runs?limit=${limit}`);
  if (!resp.ok) throw new Error(`Failed to fetch runs: ${resp.status}`);
  return resp.json();
}

export async function fetchRun(runId) {
  const resp = await fetch(`/api/runs/${runId}`);
  if (!resp.ok) throw new Error(`Failed to fetch run: ${resp.status}`);
  return resp.json();
}

export async function fetchManifests() {
  const resp = await fetch("/api/manifests");
  if (!resp.ok) throw new Error(`Failed to fetch manifests: ${resp.status}`);
  return resp.json();
}

export async function triggerRun(manifestPath) {
  const resp = await fetch("/api/runs/trigger", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ manifest_path: manifestPath }),
  });
  if (!resp.ok) {
    // FastAPI puts the reason in `detail` — surface it instead of the bare status.
    const detail = await resp.json().then((b) => b?.detail).catch(() => null);
    throw new Error(detail || `Failed to start run: ${resp.status}`);
  }
  return resp.json();
}

/** WebSocket URL for a live run, on whatever host served this page. */
export function runStreamUrl(streamId) {
  const scheme = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${scheme}//${window.location.host}/api/runs/${streamId}/stream`;
}
