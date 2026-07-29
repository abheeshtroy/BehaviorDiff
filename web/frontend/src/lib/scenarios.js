import { SCENARIOS } from "../demoData";

const CATEGORY_SURFACE = {
  http: "HTTP responses",
  postgres: "database rows",
  outbound: "outbound calls",
  latency: "timing",
};

/**
 * Find the scripted scenario that corresponds to a manifest.
 *
 * Accepts anything that ends in the manifest filename — the API returns
 * relative paths ("demo/manifests/x.yaml") while a stored run's
 * manifest_path may be absolute.
 */
export function scenarioForManifest(pathOrFilename) {
  if (!pathOrFilename) return null;
  const filename = String(pathOrFilename).split("/").pop();
  return Object.values(SCENARIOS).find((s) => s.manifest === filename) ?? null;
}

/**
 * What a viewer should keep an eye on, phrased without giving away the
 * ending — the observed surface and how much lands on it, never which
 * finding is the bad one. The reveal is the point of the demo.
 */
export function watchLine(scenario) {
  if (!scenario) return null;
  const surfaces = [...new Set(scenario.findings.map((f) => f.category))]
    .map((c) => CATEGORY_SURFACE[c] ?? c);
  if (surfaces.length === 0) return null;

  const list =
    surfaces.length === 1
      ? surfaces[0]
      : `${surfaces.slice(0, -1).join(", ")} and ${surfaces[surfaces.length - 1]}`;
  const n = scenario.findings.length;
  return `${n} difference${n === 1 ? "" : "s"} across ${list}`;
}
