import { buildGrid, RUN_LEVEL } from "../lib/blastRadius";

/**
 * Where a change landed: every workflow that produced a difference, against
 * every observation surface it landed on.
 *
 * A change that only moves HTTP status codes reads very differently from one
 * that also reached the database and the outbound calls, and a list of findings
 * doesn't show that at a glance. The arithmetic lives in lib/blastRadius.js;
 * this is the table around it.
 *
 * totalWorkflows is optional. With it, the grid can say how many workflows came
 * out identical — the rows here are only the ones that diverged.
 */

function Cell({ count }) {
  if (count === 0) {
    return (
      <td className="blast-cell blast-clean">
        <span className="blast-tick" aria-hidden="true">✓</span>
        <span className="blast-sr">identical</span>
      </td>
    );
  }
  return (
    <td className="blast-cell blast-diff">
      {count} {count === 1 ? "diff" : "diffs"}
    </td>
  );
}

export default function BlastRadiusGrid({ findings = [], noiseSummary = null, totalWorkflows = null }) {
  const suppressed =
    (noiseSummary?.http_suppressed ?? 0) + (noiseSummary?.postgres_suppressed ?? 0);
  const suppressedNote = `${suppressed} differences suppressed by normalization`;

  if (findings.length === 0) {
    return (
      <div className="blast-wrap">
        <div className="blast-empty">
          Nothing to plot — every observed surface was identical across both versions.
        </div>
        {suppressed > 0 && <div className="blast-note">{suppressedNote}</div>}
      </div>
    );
  }

  const { rows, columns, countAt, columnTotal, cellCount, cleanCells, affectedWorkflows } =
    buildGrid(findings);
  const untouched = totalWorkflows == null ? 0 : Math.max(0, totalWorkflows - affectedWorkflows);

  return (
    <div className="blast-wrap">
      <div className="blast-scroll">
        <table className="blast-table">
          <thead>
            <tr>
              <th className="blast-th blast-th-row">Workflow</th>
              {columns.map((column) => (
                <th key={column} className="blast-th">{column}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row} className="blast-row">
                <th scope="row" className={`blast-rowname ${row === RUN_LEVEL ? "blast-rowname-quiet" : ""}`}>
                  {row}
                </th>
                {columns.map((column) => (
                  <Cell key={column} count={countAt(row, column)} />
                ))}
              </tr>
            ))}
          </tbody>
          <tfoot>
            <tr>
              <th scope="row" className="blast-rowname blast-total-label">Total</th>
              {columns.map((column) => {
                const total = columnTotal(column);
                return (
                  <td
                    key={column}
                    className={`blast-cell blast-total ${total > 0 ? "blast-total-diff" : ""}`}
                  >
                    {total}
                  </td>
                );
              })}
            </tr>
          </tfoot>
        </table>
      </div>

      <div className="blast-note">
        {cleanCells} of {cellCount} surfaces identical
        {untouched > 0 && ` · ${untouched} workflow${untouched === 1 ? "" : "s"} untouched`}
        {suppressed > 0 && ` · ${suppressedNote}`}
      </div>
    </div>
  );
}
