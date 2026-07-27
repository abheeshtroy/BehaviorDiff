import { useEffect, useState } from "react";
import { useParams, Link } from "react-router-dom";
import { fetchRun } from "../api";

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

function FindingCard({ finding, index, classification }) {
  const [open, setOpen] = useState(false);
  const label = classification?.classifications?.find((c) => c.finding_index === index);

  return (
    <div className="finding-card" onClick={() => setOpen(!open)}>
      <div className={`finding-accent ${CAT_ACCENT[finding.category] || ""}`} />
      <div className="finding-body">
        <div className="finding-top">
          <span className="finding-sev">{SEV_ICON[finding.severity] || "?"}</span>
          <span className={`badge ${CAT_BADGE[finding.category] || "badge-muted"}`}>{finding.category}</span>
          {label && <span className={`badge ${CLASS_BADGE[label.classification] || "badge-muted"}`}>{label.classification}</span>}
          <span className="finding-summary">{finding.summary}</span>
          <span className="finding-chevron">{open ? "▲" : "▼"}</span>
        </div>
        <div className="finding-meta">
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

  if (loading) return <p style={{ color: "var(--text-3)", padding: "40px 0" }}>Loading…</p>;
  if (error) return <p style={{ color: "var(--red)" }}>Error: {error}</p>;
  if (!run) return <p>Not found</p>;

  const { result, intent, classification } = run;
  const findings = result.findings || [];
  const noise = result.noise_summary || {};
  const suppressed = (noise.http_suppressed || 0) + (noise.postgres_suppressed || 0);
  const suspicious = classification?.classifications?.filter(c => c.classification === "suspicious").length || 0;

  return (
    <div style={{ paddingTop: "20px" }}>
      <Link to="/runs" className="back-link">← All runs</Link>

      <div className="pr-header">
        <div className="pr-row">
          <span className="pr-badge">{run.id}</span>
          <span className="pr-title">{run.app_name}</span>
        </div>
        {intent && <div className="pr-intent">{intent.summary}</div>}
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
          {intent && (
            <div className="info-panel">
              <div className="info-label">Change intent</div>
              <div className="info-text">{intent.summary}</div>
              {intent.expected_behavior_changes?.length > 0 && (
                <div className="info-list">
                  {intent.expected_behavior_changes.map((c, i) => (
                    <div key={i} className="info-list-item">
                      <span className="info-arrow">→</span> {c}
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}

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
              <FindingCard key={i} finding={f} index={i} classification={classification} />
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
