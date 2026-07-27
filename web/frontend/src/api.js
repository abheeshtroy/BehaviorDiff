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
