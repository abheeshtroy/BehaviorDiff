import { SCENARIOS, SCENARIO_LIST } from "../demoData";

// Where the demo manifests live in the repo. Only ever shown as a label here:
// the static build has nothing to trigger them with.
const MANIFEST_DIR = "demo/manifests";

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
 * The scripted scenarios in the shape /api/manifests returns.
 *
 * Used when there is no run server to ask — the static deployment, or a local
 * one with the API down. The walkthroughs are bundled with the page, so the
 * picker can still list every comparison it knows how to narrate; it just
 * can't offer to run one. Nothing here is triggerable, so `path` is the file a
 * visitor who clones the repo would run, not a target this page can POST.
 */
export function scenarioManifests() {
  return SCENARIO_LIST.map((scenario) => ({
    path: `${MANIFEST_DIR}/${scenario.manifest}`,
    filename: scenario.manifest,
    app_name: null,
    workflow_count: scenario.workflows ?? null,
    error: null,
  }));
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
