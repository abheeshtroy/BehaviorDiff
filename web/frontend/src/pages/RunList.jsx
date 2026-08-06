import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { fetchRuns } from "../api";
import DemoBackground from "../components/DemoBackground";
import StateNotice from "../components/StateNotice";

export default function RunList() {
  const [runs, setRuns] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  useEffect(() => {
    fetchRuns()
      .then(setRuns)
      .catch(setError)
      .finally(() => setLoading(false));
  }, []);

  // A secondary page, but the same product: same background, same states.
  if (loading) {
    return (
      <div className="demo-page">
        <DemoBackground diverged={false} />
        <StateNotice variant="loading" title="Loading runs…" />
      </div>
    );
  }

  // Reachable from the topbar on the static deployment, where there is no run
  // server and never will be. Not an error there — just the wrong page.
  if (error?.offline) {
    return (
      <div className="demo-page">
        <DemoBackground diverged={false} />
        <StateNotice
          variant="notfound"
          title="This page requires a local BehaviorDiff installation"
          detail="Runs are stored by the machine that ran them. The walkthroughs are bundled with this page and work as they are."
        >
          <Link to="/runs/new" className="btn-sec act-link">Pick a comparison</Link>
        </StateNotice>
      </div>
    );
  }

  if (error) {
    return (
      <div className="demo-page">
        <DemoBackground diverged={false} />
        <StateNotice variant="error" title="Could not reach the run server" detail={error.message}>
          <Link to="/runs/new" className="btn-sec act-link">Back to scenarios</Link>
        </StateNotice>
      </div>
    );
  }

  if (runs.length === 0) {
    return (
      <div className="demo-page">
        <DemoBackground diverged={false} />
        <StateNotice
          variant="empty"
          title="No runs yet"
          detail={<>Every comparison you run for real is stored here.</>}
        >
          <Link to="/runs/new" className="btn-sec act-link">Back to scenarios</Link>
        </StateNotice>
      </div>
    );
  }

  return (
    <div className="demo-page">
      <DemoBackground diverged={false} />
      <div className="list-head">
        <div className="sec-label" style={{ marginBottom: 0 }}>Recent runs</div>
        <Link to="/runs/new" className="btn-sec stream-link-btn">Back to scenarios</Link>
      </div>
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
