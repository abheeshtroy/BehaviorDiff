import { scenarioForManifest } from "./scenarios";

/**
 * A readable headline for a stored finding, when we can be sure which one it is.
 *
 * The comparator writes summaries for an operator reading a diff: "row
 * inserted into payment_calls". For the three demo scenarios, demoData.js
 * already has the same difference described in product terms, so a run of a
 * demo manifest can borrow it.
 *
 * Matching is on (category, severity), which is unique within each scripted
 * scenario — if a scenario ever gains a second finding with the same pair,
 * the match is ambiguous and this returns nothing rather than guessing. The
 * raw summary is never discarded by callers; it stays on the card as the
 * evidence of what was actually observed.
 */
export function humanizeFinding(finding, manifestPath) {
  const scenario = scenarioForManifest(manifestPath);
  if (!scenario || !finding) return null;

  // Exact key first: written against output a real run of this manifest was
  // observed to produce, so it survives the engine and the script disagreeing
  // about how a difference surfaces.
  const keyed = scenario.readable?.[findingKey(finding)];
  if (keyed) return keyed === finding.summary ? null : keyed;

  const matches = scenario.findings.filter(
    (f) => f.category === finding.category && f.severity === finding.severity,
  );
  if (matches.length !== 1) return null;

  const match = matches[0];
  if (!match.summary || match.summary === finding.summary) return null;
  return match.summary;
}

/**
 * A stable identifier for a finding: what changed, how, and to what.
 *
 * Parses the subject back out of the comparator's summary grammar — the table
 * for postgres rows, "METHOD /path" for HTTP and outbound calls. Anything it
 * cannot parse degrades to "category:severity", which is still a usable key
 * for a scenario that only has one finding of that shape.
 */
export function findingKey(finding) {
  if (!finding) return "";
  const base = `${finding.category}:${finding.severity}`;
  const summary = finding.summary ?? "";

  const table = summary.match(/row (?:inserted into|deleted from|modified in) ([^\s(]+)/);
  if (table) return `${base}:${table[1]}`;

  const outbound = summary.match(/outbound call ([A-Z]+ \S+) made only by/);
  if (outbound) return `${base}:${outbound[1]}`;

  const http = summary.match(/^([A-Z]+ \S+):/);
  if (http) return `${base}:${http[1]}`;

  return base;
}

/**
 * What the change claimed to do, for the narrative header above the stats.
 *
 * Prefers the run's own AI-extracted intent — that is derived from the real
 * diff — and falls back to the scripted scenario when a demo manifest ran
 * without an intent pass.
 */
export function changeIntent(run) {
  const fromRun = run?.intent?.summary;
  if (fromRun) {
    return {
      summary: fromRun,
      expected: run.intent.expected_behavior_changes ?? [],
      source: "run",
    };
  }

  const scenario = scenarioForManifest(run?.manifest_path);
  if (scenario) {
    return {
      summary: scenario.intent.summary,
      expected: scenario.intent.expected,
      source: "scenario",
    };
  }

  return null;
}

/** The scripted title for a run, when its manifest is one of the demo scenarios. */
export function runHeadline(run) {
  const scenario = scenarioForManifest(run?.manifest_path);
  if (scenario) return { badge: scenario.pr, title: scenario.title };
  return { badge: run?.id, title: run?.app_name };
}
