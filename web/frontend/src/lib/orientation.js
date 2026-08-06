import { SCENARIOS } from "../demoData";

/**
 * Content for the orientation beat that runs before a scenario starts.
 *
 * The beat is derived from the scenario it precedes, never hardcoded to one
 * of them: `pr` and `title` head the card, `diff` gives the scale, `checks`
 * plus the approval count fill the left column, and `findings` fill the right
 * (classification "intended" vs everything else). Only the prose — headline,
 * sub, caption — is written by hand, and it lives with the scenario in
 * demoData.js under `orientation`.
 */

/** "4 lines changed" — every added or removed line in the scripted diff. */
function scaleOf(scenario) {
  const n = scenario.diff.filter((line) => line.type !== "ctx").length;
  return `${n} line${n === 1 ? "" : "s"} changed`;
}

/**
 * The left column: what the existing checks reported. All green, all true.
 * Approvals are a review fact rather than a CI check, so they come from the
 * scenario's orientation prose and get appended to the CI list.
 */
function checksOf(scenario) {
  const approvals = scenario.orientation?.approvals;
  if (!approvals) return scenario.checks;
  return [...scenario.checks, `${approvals} approval${approvals === 1 ? "" : "s"}`];
}

/**
 * The right column: what the run actually did. The intended change is tagged
 * apart from the rest — the contrast between the two is the whole point.
 */
function changesOf(scenario) {
  return scenario.findings.map((f) => {
    const intended = f.classification === "intended";
    return {
      tag: intended ? "intended" : "bug",
      tone: intended ? "intended" : "bug",
      text: f.summary,
    };
  });
}

/**
 * Build the beat for a scenario, by object or by id. Returns null when there
 * is no scripted scenario — callers fall back to GENERIC_BEAT, which states
 * the problem without claiming to describe a change it doesn't have.
 */
export function beatFor(scenarioOrId) {
  const scenario = typeof scenarioOrId === "string" ? SCENARIOS[scenarioOrId] : scenarioOrId;
  if (!scenario) return null;

  const prose = scenario.orientation ?? {};
  return {
    pr: scenario.pr,
    title: scenario.title,
    scale: scaleOf(scenario),
    headline: prose.headline,
    sub: prose.sub,
    checks: checksOf(scenario),
    changes: changesOf(scenario),
    caption: prose.caption,
  };
}

/**
 * Shown when the thing about to run has no scripted scenario behind it: the
 * same argument, minus the pull request card there is no data for.
 */
export const GENERIC_BEAT = {
  headline: "Every check can be green and the behaviour can still change.",
  sub: "Tests, coverage and review all report on the code. None of them report on what the code does differently once it runs. That is what this compares.",
};
