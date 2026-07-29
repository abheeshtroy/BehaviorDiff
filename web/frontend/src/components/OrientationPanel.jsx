import { CHECKOUT_BEAT } from "../lib/orientation";

/**
 * The orientation beat that runs before the diff review.
 *
 * A cold visitor arriving at a scenario has no reason to care about a
 * four-line diff yet. This states the problem first — every check green,
 * bugs shipped anyway — and only then hands over to the code.
 *
 * The content is fixed (the checkout change) rather than derived from the
 * scenario being viewed, because the panel is an illustration of what the
 * tool is for, not a summary of what is about to happen. Content lives in
 * lib/orientation.js.
 */
export default function OrientationPanel({ beat = CHECKOUT_BEAT, onContinue, continueLabel = "See the code →" }) {
  return (
    <div className="demo-pane orient-panel">
      <div className="stage-label"><span className="stage-num">00</span></div>

      <h2 className="orient-headline">{beat.headline}</h2>
      <p className="orient-sub">{beat.sub}</p>

      <div className="pitch">
        <div className="pitch-head">
          <span className="pr-badge">{beat.pr}</span>
          <span className="pitch-head-title">{beat.title}</span>
          <span className="pitch-head-dim">{beat.scale}</span>
        </div>

        <div className="pitch-cols">
          <div>
            <div className="pitch-label">What the checks saw</div>
            <div className="actual-list">
              {beat.checks.map((check) => (
                <div key={check} className="actual-row pass">
                  <span className="actual-tag actual-mark">✓</span>
                  {check}
                </div>
              ))}
            </div>
          </div>

          <div className="pitch-arrow">→</div>

          <div>
            <div className="pitch-label">What actually changed</div>
            <div className="actual-list">
              {beat.changes.map((change) => (
                <div key={change.text} className={`actual-row ${change.tone}`}>
                  <span className="actual-tag">{change.tag}</span>
                  {change.text}
                </div>
              ))}
            </div>
          </div>
        </div>

        <div className="pitch-caption">{beat.caption}</div>
      </div>

      <div className="orient-actions">
        <button className="btn-pri" onClick={onContinue}>{continueLabel}</button>
      </div>
    </div>
  );
}
