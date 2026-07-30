import { useState } from "react";
import JsonBlock from "./JsonBlock";
import { buildSequence, unattributedCount } from "../lib/sequence";

/**
 * The run in order: what it did, when, and where the two versions parted ways.
 *
 * The findings tab lists differences; this puts them back in the run that
 * produced them, so "the discount was cleared" reads as something that happened
 * at a moment, between two other things. The row-building lives in
 * lib/sequence.js; this is the spine and the labels around it.
 *
 * Diverged rows open one at a time — the evidence is wide, and two of them side
 * by side is a diff of a diff.
 */

const STATUS_BADGE = { http: "badge-blue", postgres: "badge-purple", outbound: "badge-orange" };

function SequenceRow({ row, open, onToggle }) {
  const expandable = row.status === "diverge";
  const finding = row.finding;

  return (
    <div className={`seq-row seq-${row.status} ${open ? "seq-open" : ""}`}>
      <div
        className="seq-line"
        onClick={expandable ? onToggle : undefined}
        role={expandable ? "button" : undefined}
        tabIndex={expandable ? 0 : undefined}
        aria-expanded={expandable ? open : undefined}
        onKeyDown={
          expandable
            ? (e) => {
                if (e.key === "Enter" || e.key === " ") {
                  e.preventDefault();
                  onToggle();
                }
              }
            : undefined
        }
      >
        <div className="seq-time">{row.elapsed}</div>
        <div className="seq-spine">
          <span className="seq-dot" />
        </div>
        <div className="seq-body">
          <span className="seq-icon" aria-hidden="true">{row.icon}</span>
          <span className="seq-label">{row.label}</span>
          {finding && (
            <>
              <span className={`badge ${STATUS_BADGE[finding.category] ?? "badge-muted"}`}>
                {finding.category}
              </span>
              <span className="seq-summary">{finding.summary}</span>
              {row.findingCount > 1 && (
                <span className="seq-more">+{row.findingCount - 1} more</span>
              )}
              <span className="seq-chevron">{open ? "▲" : "▼"}</span>
            </>
          )}
        </div>
      </div>

      {open && finding && (
        <div className="seq-detail">
          <div className="evidence seq-evidence">
            <JsonBlock label="Base" data={finding.evidence_base} variant="base" />
            <JsonBlock label="Target" data={finding.evidence_target} variant="target" />
          </div>
        </div>
      )}
    </div>
  );
}

export default function SequenceDiagram({ events = null, findings = [] }) {
  const [openIndex, setOpenIndex] = useState(null);
  const rows = buildSequence(events, findings);

  if (rows.length === 0) {
    // Either an old run stored before the event stream was persisted, or a
    // stream with nothing in it worth a row. Say so rather than draw an empty
    // spine that reads as "the run did nothing".
    return (
      <div className="seq-wrap">
        <div className="seq-empty">No event stream recorded for this run</div>
      </div>
    );
  }

  const diverged = rows.filter((row) => row.status === "diverge").length;
  const unattributed = unattributedCount(rows, findings);

  return (
    <div className="seq-wrap">
      <div className="seq-rows">
        {rows.map((row, index) => (
          <SequenceRow
            key={`${row.stage}-${index}`}
            row={row}
            open={openIndex === index}
            onToggle={() => setOpenIndex(openIndex === index ? null : index)}
          />
        ))}
      </div>

      <div className="seq-note">
        {rows.length} steps · {diverged === 0 ? "none diverged" : `${diverged} diverged`}
        {/* A finding the stream cannot place must still be accounted for, or the
            spine quietly under-reports what the run found. */}
        {unattributed > 0 &&
          ` · ${unattributed} finding${unattributed === 1 ? "" : "s"} not tied to a step`}
      </div>
    </div>
  );
}
