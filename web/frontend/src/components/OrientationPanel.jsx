import { GENERIC_BEAT } from "../lib/orientation";

/**
 * The orientation beat that runs before the diff review.
 *
 * A cold visitor arriving at a scenario has no reason to care about a
 * four-line diff yet. This states the problem first — every check green,
 * bugs shipped anyway — and only then hands over to the code.
 *
 * The beat describes the scenario about to run, so callers pass the one built
 * by beatFor() in lib/orientation.js. Without a beat it falls back to the
 * generic argument and drops the pull request card rather than showing some
 * other change's numbers.
 */
export default function OrientationPanel({ beat, onContinue, continueLabel = "See the code →" }) {
  const b = beat ?? GENERIC_BEAT;
  const hasPitch = Boolean(b.checks?.length && b.changes?.length);

  return (
    <div className="demo-pane orient-panel">
      <div className="stage-label"><span className="stage-num">00</span></div>

      <h2 className="orient-headline">{b.headline}</h2>
      <p className="orient-sub">{b.sub}</p>

      {hasPitch && (
      <div className="pitch">
        <div className="pitch-head">
          <span className="pr-badge">{b.pr}</span>
          <span className="pitch-head-title">{b.title}</span>
          <span className="pitch-head-dim">{b.scale}</span>
        </div>

        <div className="pitch-cols">
          <div>
            <div className="pitch-label">What the checks saw</div>
            <div className="actual-list">
              {b.checks.map((check) => (
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
              {b.changes.map((change) => (
                <div key={change.text} className={`actual-row ${change.tone}`}>
                  <span className="actual-tag">{change.tag}</span>
                  {change.text}
                </div>
              ))}
            </div>
          </div>
        </div>

        <div className="pitch-caption">{b.caption}</div>
      </div>
      )}

      <div className="orient-actions">
        <button className="btn-pri" onClick={onContinue}>{continueLabel}</button>
      </div>
    </div>
  );
}
