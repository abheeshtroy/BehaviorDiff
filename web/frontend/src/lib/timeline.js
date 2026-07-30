/**
 * The same run the sequence view lists, laid out against a horizontal clock.
 *
 * The rows come from lib/sequence.js — a step is a step whichever way it is
 * drawn, and the two views disagreeing about what happened would be worse than
 * either being wrong on its own. What is new here is the geometry: where a step
 * sits along the bar, and which step a cursor at some pixel is nearest to.
 *
 * The maths is kept out of the component so it can be tested without a DOM, and
 * so the component can ask for positions in whatever unit it is drawing in —
 * pass a barWidth of 100 and the offsets come back as percentages.
 */

import { buildSequence } from "./sequence";

/** A run's total wall time, at the precision a reader can use. */
export function formatDuration(ms) {
  if (!(ms > 0)) return "0ms";
  if (ms < 1000) return `${Math.round(ms)}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

/**
 * Where an event falls along a bar spanning firstTimestamp → lastTimestamp.
 *
 * A run whose events all share one timestamp has no span to spread them over,
 * so everything sits at the left edge rather than dividing by zero. Positions
 * are clamped: an event outside the bounds pins to an edge instead of being
 * drawn off the end of the bar.
 */
export function positionForEvent(event, firstTimestamp, lastTimestamp, barWidth) {
  const span = lastTimestamp - firstTimestamp;
  if (!(span > 0)) return 0;
  const fraction = (event.timestamp - firstTimestamp) / span;
  return Math.min(1, Math.max(0, fraction)) * barWidth;
}

/**
 * The index of the event nearest a cursor sitting at cursorX pixels along the
 * bar — the cursor snaps to real events, so the detail panel always describes
 * something that actually happened rather than an interpolated moment.
 *
 * Ties go to the earlier event. Returns -1 when there is nothing to snap to.
 */
export function nearestEvent(cursorX, events, firstTimestamp, lastTimestamp, barWidth) {
  if (!events || events.length === 0) return -1;

  let best = 0;
  let bestDistance = Infinity;

  events.forEach((event, index) => {
    const distance = Math.abs(positionForEvent(event, firstTimestamp, lastTimestamp, barWidth) - cursorX);
    if (distance < bestDistance) {
      bestDistance = distance;
      best = index;
    }
  });

  return best;
}

/**
 * The run as marks on a clock: the sequence rows, plus the bounds to place them
 * against.
 *
 * The bar spans the whole run — the first event to the last — not just the
 * steps worth a mark. Standing the two versions up is most of a real run's wall
 * time, and a bar that quietly dropped it would misstate when everything else
 * happened.
 */
export function buildTimeline(events, findings = []) {
  const marks = buildSequence(events, findings);
  if (marks.length === 0) return { marks: [], start: 0, end: 0, durationMs: 0 };

  const start = events[0].timestamp;
  const end = Math.max(events[events.length - 1].timestamp, marks[marks.length - 1].timestamp);

  return { marks, start, end, durationMs: (end - start) * 1000 };
}
