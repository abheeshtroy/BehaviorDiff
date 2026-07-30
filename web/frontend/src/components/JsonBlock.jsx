/**
 * One side of a finding's evidence — the raw base or target payload.
 *
 * Lived inside RunDetail until the sequence view needed to show the same thing;
 * evidence should look identical wherever it is opened, so there is one of it.
 */
export default function JsonBlock({ label, data, variant }) {
  if (data == null) return null;
  return (
    <div>
      <div className="ev-label">
        <span className={`ev-dot ev-dot-${variant}`} />
        {label}
      </div>
      <pre className="ev-pre">
        {typeof data === "string" ? data : JSON.stringify(data, null, 2)}
      </pre>
    </div>
  );
}
