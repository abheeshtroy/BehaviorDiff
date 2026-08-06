import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import JsonBlock from "./JsonBlock";
import { buildTimeline, formatDuration, nearestEvent, positionForEvent } from "../lib/timeline";

/**
 * The run against a clock, scrubbed like a tape.
 *
 * The sequence view answers "what happened, in what order"; this answers "when,
 * and how much of the run went where". Reading a list tells you the discount
 * was cleared after the checkout workflow. Dragging a cursor tells you the whole
 * run was over in four seconds and half of that was standing the versions up.
 *
 * The cursor only ever rests on a real step — an interpolated moment has no
 * evidence to show — so the state here is which step it is on, and the pixel
 * geometry lives in lib/timeline.js.
 */

const STATUS_BADGE = { http: "badge-blue", postgres: "badge-purple", outbound: "badge-orange" };

/** Positions are asked for against a bar 100 wide, i.e. in percent. */
const pct = (mark, start, end) => positionForEvent(mark, start, end, 100);

function MarkDetail({ mark }) {
  const finding = mark.finding;

  return (
    <div className={`tl-detail tl-detail-${mark.status}`}>
      <div className="tl-detail-head">
        <span className="tl-detail-time">{mark.elapsed}</span>
        <span className="tl-detail-icon" aria-hidden="true">{mark.icon}</span>
        <span className="tl-detail-label">{mark.label}</span>
        {finding && (
          <span className={`badge ${STATUS_BADGE[finding.category] ?? "badge-muted"}`}>
            {finding.category}
          </span>
        )}
      </div>

      {/* The label is the short form the bar needs; here there is room for what
          the pipeline actually said, unless it said the same thing. */}
      {mark.message && mark.message !== mark.label && (
        <div className="tl-detail-msg">{mark.message}</div>
      )}

      {finding && (
        <>
          <div className="tl-detail-summary">
            {finding.summary}
            {mark.findingCount > 1 && (
              <span className="tl-detail-more"> +{mark.findingCount - 1} more here</span>
            )}
          </div>
          <div className="evidence tl-evidence">
            <JsonBlock label="Base" data={finding.evidence_base} variant="base" />
            <JsonBlock label="Target" data={finding.evidence_target} variant="target" />
          </div>
        </>
      )}
    </div>
  );
}

export default function TimelineScrubber({ events = null, findings = [] }) {
  const barRef = useRef(null);
  const [index, setIndex] = useState(0);
  const [dragging, setDragging] = useState(false);

  const { marks, start, end, durationMs, leadMs } = useMemo(
    () => buildTimeline(events, findings),
    [events, findings],
  );

  // A shorter stream than the one the cursor was placed against must not leave
  // the cursor pointing past the end of the bar.
  const at = Math.min(index, Math.max(0, marks.length - 1));

  const moveTo = useCallback(
    (clientX) => {
      const bar = barRef.current;
      if (!bar) return;
      const rect = bar.getBoundingClientRect();
      const next = nearestEvent(clientX - rect.left, marks, start, end, rect.width);
      if (next >= 0) setIndex(next);
    },
    [marks, start, end],
  );

  // Dragging is tracked on the document, not the bar: a cursor dragged past the
  // end of the bar should pin to the end, not be dropped there.
  useEffect(() => {
    if (!dragging) return;

    const onMove = (e) => {
      const point = e.touches ? e.touches[0] : e;
      if (!point) return;
      // Stops a touch drag scrolling the page underneath it.
      if (e.cancelable) e.preventDefault();
      moveTo(point.clientX);
    };
    const onRelease = () => setDragging(false);

    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onRelease);
    document.addEventListener("touchmove", onMove, { passive: false });
    document.addEventListener("touchend", onRelease);
    document.addEventListener("touchcancel", onRelease);

    return () => {
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onRelease);
      document.removeEventListener("touchmove", onMove);
      document.removeEventListener("touchend", onRelease);
      document.removeEventListener("touchcancel", onRelease);
    };
  }, [dragging, moveTo]);

  if (marks.length === 0) {
    // Same answer the sequence view gives: an old run stored before the stream
    // was persisted, or a stream with nothing in it worth a mark.
    return (
      <div className="tl-wrap">
        <div className="tl-empty">No event stream recorded for this run</div>
      </div>
    );
  }

  const current = marks[at];
  const cursorPct = pct(current, start, end);
  const diverged = marks.filter((mark) => mark.status === "diverge").length;

  const onKeyDown = (e) => {
    if (e.key === "ArrowLeft" || e.key === "ArrowRight" || e.key === "Home" || e.key === "End") {
      e.preventDefault();
    }
    if (e.key === "ArrowLeft") setIndex(Math.max(0, at - 1));
    else if (e.key === "ArrowRight") setIndex(Math.min(marks.length - 1, at + 1));
    else if (e.key === "Home") setIndex(0);
    else if (e.key === "End") setIndex(marks.length - 1);
  };

  const onMouseDown = (e) => {
    // Without this a drag across the bar selects the labels around it — but it
    // also suppresses the focus the click would have given the bar, so the
    // arrow keys have to be handed the bar explicitly.
    e.preventDefault();
    barRef.current?.focus();
    setDragging(true);
    moveTo(e.clientX);
  };

  const onTouchStart = (e) => {
    const point = e.touches[0];
    if (!point) return;
    setDragging(true);
    moveTo(point.clientX);
  };

  return (
    <div className={`tl-wrap ${dragging ? "tl-dragging" : ""}`}>
      {/* Both ends read as offsets from the start of the run, the way the marks
          do — the bar opens at the first mark, so its left edge is however long
          standing the two versions up took, not zero. */}
      <div className="tl-axis">
        <span>{formatDuration(leadMs)}</span>
        <span>{formatDuration(leadMs + durationMs)}</span>
      </div>

      <div
        ref={barRef}
        className="tl-bar"
        role="slider"
        tabIndex={0}
        aria-label="Run timeline"
        aria-valuemin={0}
        aria-valuemax={marks.length - 1}
        aria-valuenow={at}
        aria-valuetext={`${current.elapsed} · ${current.label}`}
        onMouseDown={onMouseDown}
        onTouchStart={onTouchStart}
        onKeyDown={onKeyDown}
      >
        {/* The run before its first observed step — building and booting both
            versions. Shaded rather than left blank, so the gap reads as time
            spent rather than as a bar that failed to draw. */}
        {pct(marks[0], start, end) > 0 && (
          <div className="tl-lead" style={{ width: `${pct(marks[0], start, end)}%` }} />
        )}

        {marks.map((mark, i) => (
          <span
            key={`${mark.stage}-${i}`}
            className={`tl-tick tl-tick-${mark.status} ${i === at ? "tl-tick-at" : ""}`}
            style={{ left: `${pct(mark, start, end)}%` }}
            aria-hidden="true"
          />
        ))}

        <div className="tl-cursor" style={{ left: `${cursorPct}%` }}>
          <span className="tl-handle" />
        </div>
      </div>

      {/* Keyed on the step so a move remounts the panel and replays its fade;
          without it the evidence would swap under the reader's eyes. */}
      <MarkDetail key={at} mark={current} />

      <div className="tl-note">
        Step {at + 1} of {marks.length} · {diverged === 0 ? "none diverged" : `${diverged} diverged`}
        <span className="tl-hint">drag the cursor, or use ← →</span>
      </div>
    </div>
  );
}
