import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { fetchRuns } from "../api";

export default function RunList() {
  const [runs, setRuns] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  useEffect(() => {
    fetchRuns()
      .then(setRuns)
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }, []);

  if (loading) return <p style={{ color: "var(--text-3)", padding: "40px 0" }}>Loading…</p>;
  if (error) return <p style={{ color: "var(--red)" }}>Error: {error}</p>;
  if (runs.length === 0) {
    return (
      <div className="empty-state">
        <div className="empty-icon">◇</div>
        <p className="empty-title">No runs yet</p>
        <p className="empty-hint">Run <code>behaviordiff manifest.yaml</code> to see results here</p>
      </div>
    );
  }

  return (
    <div style={{ paddingTop: "32px" }}>
      <div className="sec-label">Recent runs</div>
      {runs.map((run) => (
        <Link key={run.id} to={`/runs/${run.id}`} className="run-card">
          <div className="run-left">
            <div className="run-title-row">
              <span className="run-app">{run.app_name}</span>
              <span className="run-id">{run.id}</span>
            </div>
            <div className="run-meta">
              {run.manifest_path} · {new Date(run.created_at).toLocaleString()}
            </div>
          </div>
          <div className="run-right">
            {run.total_findings > 0 ? (
              <span className="badge badge-red">
                {run.total_findings} finding{run.total_findings !== 1 ? "s" : ""}
              </span>
            ) : (
              <span className="badge badge-green">clean</span>
            )}
            {run.total_suppressed > 0 && (
              <span className="run-stat">{run.total_suppressed} suppressed</span>
            )}
            <span className="run-stat">{run.total_workflows}w · {run.total_steps}s</span>
            <span className="run-stat">{run.duration_seconds?.toFixed(1)}s</span>
          </div>
        </Link>
      ))}
    </div>
  );
}
