import { useEffect, useRef, useState } from "react";
import { useParams, useNavigate, Link } from "react-router-dom";
import { SCENARIOS } from "../demoData";
import DemoBackground from "../components/DemoBackground";

const PHASES = [
  "starting containers",
  "seeding both databases",
  "running workflows",
  "comparing observations",
  "classifying findings",
];

const CAT_TAG = { http: "badge-blue", postgres: "badge-purple", outbound: "badge-orange", latency: "badge-muted" };
const CAT_ACCENT = { http: "accent-http", postgres: "accent-postgres", outbound: "accent-outbound", latency: "accent-latency" };
const CLS_TAG = { intended: "badge-green", suspicious: "badge-red", noise: "badge-muted" };
const SEV = { changed: "~", added: "+", removed: "−" };

function Json({ label, data, variant }) {
  if (data == null) return null;
  return (
    <div>
      <div className="ev-label"><span className={`ev-dot ev-dot-${variant}`} />{label}</div>
      <pre className="ev-pre">{JSON.stringify(data, null, 2)}</pre>
    </div>
  );
}

function Finding({ f, index, visible, autoOpen }) {
  const [open, setOpen] = useState(autoOpen);
  return (
    <div
      className={`finding-card demo-finding ${visible ? "in" : ""}`}
      onClick={() => setOpen(!open)}
      style={{ transitionDelay: `${index * 0.11}s` }}
    >
      <div className={`finding-accent ${CAT_ACCENT[f.category]}`} />
      <div className="finding-body">
        <div className="finding-top">
          <span className="finding-sev">{SEV[f.severity]}</span>
          <span className={`badge ${CAT_TAG[f.category]}`}>{f.category}</span>
          <span className={`badge ${CLS_TAG[f.classification]}`}>{f.classification}</span>
          <span className="finding-summary">{f.summary}</span>
          <span className="finding-chevron">{open ? "▲" : "▼"}</span>
        </div>
        <div className="finding-meta">
          {f.workflow && (
            <div className="finding-workflow">
              {f.workflow}{f.step != null && ` · step ${f.step}`}
            </div>
          )}
          <div className="finding-reasoning">
            {f.reasoning}<span className="finding-conf"> · {(f.confidence * 100).toFixed(0)}%</span>
          </div>
        </div>
        {open && (
          <div className="evidence">
            <Json label="main" data={f.base} variant="base" />
            <Json label="target" data={f.target} variant="target" />
          </div>
        )}
      </div>
    </div>
  );
}

export default function DemoRun() {
  const { scenarioId } = useParams();
  const navigate = useNavigate();
  const s = SCENARIOS[scenarioId];

  const [stage, setStage] = useState("review");
  const [choice, setChoice] = useState(null);
  const [phase, setPhase] = useState(0);
  const [elapsed, setElapsed] = useState(0);
  const [litSteps, setLitSteps] = useState(-1);
  const [diverged, setDiverged] = useState(false);
  const [shownFindings, setShownFindings] = useState(0);
  const [showNoise, setShowNoise] = useState(false);
  const [counts, setCounts] = useState({ w: 0, d: 0, s: 0 });
  const timers = useRef([]);

  useEffect(() => () => timers.current.forEach(clearTimeout), []);

  if (!s) return <p style={{ padding: "40px 0", color: "var(--text-3)" }}>Scenario not found.</p>;

  const suspicious = s.findings.filter((f) => f.classification === "suspicious").length;

  function schedule(fn, ms) {
    timers.current.push(setTimeout(fn, ms));
  }

  function start(decision) {
    setChoice(decision);
    setStage("running");

    const tick = setInterval(() => setElapsed((e) => e + 0.1), 100);
    timers.current.push(tick);

    PHASES.forEach((_, i) => schedule(() => setPhase(i), i * 780));

    s.steps.forEach((_, i) => {
      const before = Math.min(i, s.divergeAt);
      const after = Math.max(0, i - s.divergeAt);
      schedule(() => {
        setLitSteps(i);
        if (i === s.divergeAt) setDiverged(true);
      }, 1200 + before * 430 + after * 500);
    });

    schedule(() => {
      clearInterval(tick);
      setStage("results");
      animateCounts();
      s.findings.forEach((_, i) => schedule(() => setShownFindings(i + 1), 450 + i * 520));
      schedule(() => setShowNoise(true), 450 + s.findings.length * 520);
    }, 5200);
  }

  function animateCounts() {
    const dur = 900;
    const t0 = performance.now();
    function frame(now) {
      const p = Math.min((now - t0) / dur, 1);
      setCounts({
        w: Math.round(p * s.workflows),
        d: Math.round(p * s.findings.length),
        s: Math.round(p * suspicious),
      });
      if (p < 1) requestAnimationFrame(frame);
    }
    requestAnimationFrame(frame);
  }

  function reset() {
    timers.current.forEach(clearTimeout);
    timers.current = [];
    setStage("review"); setChoice(null); setPhase(0); setElapsed(0);
    setLitSteps(-1); setDiverged(false); setShownFindings(0);
    setShowNoise(false); setCounts({ w: 0, d: 0, s: 0 });
  }

  return (
    <div className="demo-page">
      <DemoBackground diverged={diverged} />
      <Link to="/" className="back-link">← Demo scenarios</Link>

      {stage === "review" && (
        <div className="demo-pane">
          <div className="stage-label"><span className="stage-num">01</span> Review the pull request</div>
          <div className="pr-row">
            <span className="pr-badge">{s.pr}</span>
            <span className="pr-title">{s.title}</span>
          </div>
          <div className="pr-intent">{s.subtitle}</div>

          <div className="code-block">
            <div className="code-file">{s.file}</div>
            {s.diff.map((l) => (
              <div className={`code-line code-${l.type}`} key={l.n}>
                <span className="code-num">{l.n}</span>
                <span className="code-text">
                  {l.type === "add" ? "+" : l.type === "rem" ? "−" : " "}{l.text}
                </span>
              </div>
            ))}
          </div>

          <div className="check-row">
            {s.checks.map((c, i) => (
              <span key={i} className={`check ${i === 1 ? "check-info" : "check-ok"}`}>
                {i !== 1 && "✓ "}{c}
              </span>
            ))}
          </div>

          <div className="ask-line">Would you approve this?</div>
          <div className="ask-btns">
            <button className="btn-approve" onClick={() => start("approved")}>Approve</button>
            <button className="btn-sec" onClick={() => start("changes")}>Request changes</button>
          </div>
        </div>
      )}

      {stage === "running" && (
        <div className="demo-pane">
          <div className="stage-label"><span className="stage-num">02</span> Both versions, identical conditions</div>

          <div className="lane">
            <span className="lane-name">main</span>
            <div className="lane-track">
              {s.steps.map((st, i) => (
                <div key={i} className={`lane-blk ${i <= litSteps ? "in ok" : ""}`}>{st}</div>
              ))}
            </div>
          </div>

          <div className={`diverge-marker ${diverged ? "in" : ""}`}>
            <span>behaviour diverges</span><span className="diverge-line" />
          </div>

          <div className="lane">
            <span className="lane-name">{s.pr.replace("#", "pr/")}</span>
            <div className="lane-track">
              {s.steps.map((st, i) => (
                <div key={i} className={`lane-blk ${i <= litSteps ? (i >= s.divergeAt ? "in bad" : "in ok") : ""}`}>{st}</div>
              ))}
            </div>
          </div>

          <div className="run-meter">
            <span className="run-time">{elapsed.toFixed(1)}s</span>
            <div className="run-bar"><div className="run-fill" style={{ width: `${((phase + 1) / PHASES.length) * 100}%` }} /></div>
            <span className="run-phase">{PHASES[phase]}</span>
          </div>
        </div>
      )}

      {stage === "results" && (
        <div className="demo-pane">
          <div className="verdict">
            <span className="verdict-icon">⚠</span>
            <span>
              {choice === "approved"
                ? `You approved it. Every check was green. ${suspicious} regression${suspicious !== 1 ? "s" : ""} shipped anyway.`
                : `Every check passed, so review would have cleared it. ${suspicious} regression${suspicious !== 1 ? "s" : ""} were live.`}
            </span>
          </div>

          <div className="stats-grid">
            <div className="stat-card"><div className="stat-label">Workflows</div><div className="stat-val">{counts.w}</div></div>
            <div className="stat-card"><div className="stat-label">Differences</div><div className="stat-val">{counts.d}</div></div>
            <div className={`stat-card ${suspicious > 0 ? "danger" : ""}`}><div className="stat-label">Suspicious</div><div className="stat-val">{counts.s}</div></div>
            <div className="stat-card success"><div className="stat-label">Checks</div><div className="stat-val" style={{ fontSize: "14px" }}>All green</div></div>
          </div>

          <div className="info-panel">
            <div className="info-label">Change intent</div>
            <div className="info-text">{s.intent.summary}</div>
            <div className="info-list">
              {s.intent.expected.map((e, i) => (
                <div key={i} className="info-list-item"><span className="info-arrow">→</span> {e}</div>
              ))}
            </div>
          </div>

          <div className="info-panel">
            <div className="info-label">Assessment</div>
            <div className="info-text">{s.assessment}</div>
          </div>

          <div className="findings-bar">
            <div className="findings-title">Findings <span className="findings-count">{s.findings.length}</span></div>
            <span className="findings-sup">ran in {s.duration}</span>
          </div>

          {s.findings.map((f, i) => (
            <Finding key={i} f={f} index={i} visible={i < shownFindings} autoOpen={i === 0} />
          ))}

          <div className={`noise-card demo-noise ${showNoise ? "in" : ""}`}>
            ≡ {s.suppressed} differences normalized — ids, timestamps, row ordering
          </div>

          <div className="results-actions">
            <button className="btn-sec" onClick={reset}>Try again</button>
            <button className="btn-sec" onClick={() => navigate("/")}>Other scenarios</button>
          </div>
        </div>
      )}
    </div>
  );
}
