import { useEffect, useState } from "react";
import { useParams, Link } from "react-router-dom";
import { fetchRun } from "../api";
import DemoBackground from "../components/DemoBackground";
import StateNotice from "../components/StateNotice";
import { changeIntent, humanizeFinding, runHeadline } from "../lib/findings";

const CAT_BADGE = { http: "badge-blue", postgres: "badge-purple", outbound: "badge-orange", latency: "badge-muted" };
const CAT_ACCENT = { http: "accent-http", postgres: "accent-postgres", outbound: "accent-outbound", latency: "accent-latency" };
const CLASS_BADGE = { intended: "badge-green", suspicious: "badge-red", noise: "badge-muted" };
const SEV_ICON = { changed: "~", added: "+", removed: "−" };

function JsonBlock({ label, data, variant }) {
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

function FindingCard({ finding, index, classification, manifestPath }) {
  const [open, setOpen] = useState(false);
  const label = classification?.classifications?.find((c) => c.finding_index === index);
  // A readable headline when the run is one of the scripted scenarios. The
  // comparator's own summary stays on the card either way — it is the record
  // of what was actually observed, and it is what a repro would quote.
  const readable = humanizeFinding(finding, manifestPath);

  return (
    <div className="finding-card" onClick={() => setOpen(!open)}>
      <div className={`finding-accent ${CAT_ACCENT[finding.category] || ""}`} />
      <div className="finding-body">
        <div className="finding-top">
          <span className="finding-sev">{SEV_ICON[finding.severity] || "?"}</span>
          <span className={`badge ${CAT_BADGE[finding.category] || "badge-muted"}`}>{finding.category}</span>
          {label && <span className={`badge ${CLASS_BADGE[label.classification] || "badge-muted"}`}>{label.classification}</span>}
          <span className="finding-summary">{readable || finding.summary}</span>
          <span className="finding-chevron">{open ? "▲" : "▼"}</span>
        </div>
        <div className="finding-meta">
          {readable && <div className="finding-raw">{finding.summary}</div>}
          {finding.workflow_name && (
            <div className="finding-workflow">
              {finding.workflow_name}{finding.step_index != null && ` · step ${finding.step_index}`}
            </div>
          )}
          {label && (
            <div className="finding-reasoning">
              {label.reasoning}<span className="finding-conf"> · {(label.confidence * 100).toFixed(0)}%</span>
            </div>
          )}
        </div>
        {open && (
          <>
            <div className="evidence">
              <JsonBlock label="Base" data={finding.evidence_base} variant="base" />
              <JsonBlock label="Target" data={finding.evidence_target} variant="target" />
            </div>
            <div className="finding-actions">
              <button className="finding-act">
                {label ? `Why ${label.classification}?` : "Details"}
              </button>
              <button className="finding-act">Copy repro</button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}

export default function RunDetail() {
  const { runId } = useParams();
  const [run, setRun] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [activeTab, setActiveTab] = useState("findings");

  useEffect(() => {
    fetchRun(runId)
      .then(setRun)
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }, [runId]);

  if (loading) {
    return (
      <div className="demo-page">
        <DemoBackground diverged={false} />
        <StateNotice variant="loading" title="Loading run…" />
      </div>
    );
  }

  if (error || !run) {
    return (
      <div className="demo-page">
        <DemoBackground diverged={false} />
        <Link to="/runs/new" className="back-link">← Back to scenarios</Link>
        <StateNotice
          variant={error ? "error" : "notfound"}
          title={error ? "Could not load this run" : "That run does not exist"}
          detail={error || `No run is stored with id ${runId}.`}
        >
          <Link to="/runs" className="btn-sec act-link">All runs</Link>
        </StateNotice>
      </div>
    );
  }

  const { result, classification } = run;
  const findings = result.findings || [];
  const noise = result.noise_summary || {};
  const suppressed = (noise.http_suppressed || 0) + (noise.postgres_suppressed || 0);
  const suspicious = classification?.classifications?.filter(c => c.classification === "suspicious").length || 0;
  const headline = runHeadline(run);
  const intent = changeIntent(run);

  return (
    <div className="demo-page">
      <DemoBackground diverged={false} />
      <div className="detail-nav">
        <Link to="/runs/new" className="back-link">← Back to scenarios</Link>
        <Link to="/runs" className="detail-nav-quiet">All runs</Link>
      </div>

      <div className="pr-header">
        <div className="pr-row">
          <span className="pr-badge">{headline.badge}</span>
          <span className="pr-title">{headline.title}</span>
        </div>
        {/* The claim comes before the evidence, so the findings below read as
            answers to it rather than as a list of unrelated differences. */}
        {intent && (
          <div className="claim-line">
            <span className="claim-label">This change claimed:</span> {intent.summary}
          </div>
        )}
        {intent?.expected?.length > 0 && (
          <div className="claim-expected">
            {intent.expected.map((e, i) => (
              <div key={i} className="info-list-item"><span className="info-arrow">→</span> {e}</div>
            ))}
          </div>
        )}
      </div>

      <div className="stats-grid">
        <div className="stat-card">
          <div className="stat-label">Workflows</div>
          <div className="stat-val">{run.total_workflows}</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Differences</div>
          <div className="stat-val">{findings.length}</div>
        </div>
        <div className={`stat-card ${suspicious > 0 ? "danger" : ""}`}>
          <div className="stat-label">Suspicious</div>
          <div className="stat-val">{suspicious}</div>
        </div>
        <div className="stat-card success">
          <div className="stat-label">CI status</div>
          <div className="stat-val" style={{ fontSize: "14px" }}>All passing</div>
        </div>
      </div>

      <div className="view-tabs">
        {["findings", "sequence", "blast radius", "timeline"].map(tab => (
          <div
            key={tab}
            className={`view-tab ${activeTab === tab ? "active" : ""}`}
            onClick={() => setActiveTab(tab)}
          >
            {tab.charAt(0).toUpperCase() + tab.slice(1)}
          </div>
        ))}
      </div>

      {activeTab === "findings" && (
        <>
          {/* Change intent used to be repeated here; it now leads the page. */}
          {classification?.summary && (
            <div className="info-panel">
              <div className="info-label">Assessment</div>
              <div className="info-text">{classification.summary}</div>
            </div>
          )}

          <div className="findings-bar">
            <div className="findings-title">
              Findings <span className="findings-count">{findings.length}</span>
            </div>
            {suppressed > 0 && <span className="findings-sup">{suppressed} suppressed by normalization</span>}
          </div>

          {findings.length === 0 ? (
            <div className="clean-state">No behavioral differences found</div>
          ) : (
            findings.map((f, i) => (
              <FindingCard
                key={i}
                finding={f}
                index={i}
                classification={classification}
                manifestPath={run.manifest_path}
              />
            ))
          )}

          {suppressed > 0 && (
            <div className="noise-card">
              ≡ {suppressed} differences normalized — ids, timestamps, ordering
            </div>
          )}
        </>
      )}

      {activeTab !== "findings" && (
        <div className="coming-soon">
          {activeTab.charAt(0).toUpperCase() + activeTab.slice(1)} view coming soon
        </div>
      )}
    </div>
  );
}
