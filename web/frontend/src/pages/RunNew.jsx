import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { fetchManifests, runStreamUrl, triggerRun } from "../api";
import DemoBackground from "../components/DemoBackground";
import OrientationPanel from "../components/OrientationPanel";
import StateNotice from "../components/StateNotice";
import { scenarioForManifest, watchLine } from "../lib/scenarios";
import { beatFor } from "../lib/orientation";
import { noteRunFailure, realRunsAvailable } from "../lib/runMode";
import { hasDiverged } from "../lib/stream";
import { phaseStates } from "../lib/phases";

// The stream is a real log, not a fixed sequence: the pipeline yields one
// workflow_started/workflow_completed pair per workflow, skips stages that
// don't apply, and grows new ones over time. So nothing here enumerates
// stages — every frame that arrives gets a row, in arrival order.

// Matches WS_UNKNOWN_RUN in web/api.py: the run id isn't registered.
const WS_UNKNOWN_RUN = 4004;

// status: connecting → streaming → done | failed, or unknown-run / lost.
const IN_FLIGHT = new Set(["connecting", "streaming"]);

/** The four phases, as a checklist. The log underneath is the detail. */
function PhaseList({ phases }) {
  return (
    <ol className="phase-list">
      {phases.map((phase) => (
        <li key={phase.id} className={`phase phase-${phase.state}`}>
          <span className="phase-mark">
            {phase.state === "done" && "✓"}
            {phase.state === "failed" && "✕"}
            {phase.state === "active" && <span className="phase-spin" />}
          </span>
          <span className="phase-label">{phase.label}</span>
        </li>
      ))}
    </ol>
  );
}

function LogRow({ event, state, t0 }) {
  const [open, setOpen] = useState(false);
  const data = event.data && Object.keys(event.data).length > 0 ? event.data : null;
  const elapsed = t0 != null ? `+${Math.max(0, event.timestamp - t0).toFixed(1)}s` : "";

  return (
    <li className="log-row">
      <div
        className={`log-line ${data ? "log-line-open" : ""}`}
        onClick={data ? () => setOpen(!open) : undefined}
      >
        <span className={`log-dot log-dot-${state}`} />
        <span className="log-time">{elapsed}</span>
        <span className="log-stage">{event.stage}</span>
        <span className="log-msg">{event.message}</span>
        {data && <span className="log-chevron">{open ? "▲" : "▼"}</span>}
      </div>
      {open && data && <pre className="ev-pre log-data">{JSON.stringify(data, null, 2)}</pre>}
    </li>
  );
}

/**
 * One comparison, told as a change rather than as a file.
 *
 * The walkthrough is the offer: it needs nothing installed and always
 * finishes, so it is the only primary action here. Running the same
 * comparison for real is a demoted second option, because it needs a Docker
 * daemon and most visitors don't have one. Manifest paths are deliberately
 * absent — they identify the file to the operator, not the change to the
 * visitor. The one exception is a manifest that won't parse, where naming
 * the file is the whole point of the message.
 */
function ComparisonCard({ manifest, selected, canRunReal, onRun }) {
  const scenario = scenarioForManifest(manifest.filename);
  const broken = Boolean(manifest.error);
  const runnable = canRunReal && !broken;

  return (
    <div className={`comparison-card ${selected ? "selected" : ""}`}>
      <div className="comparison-main">
        <div className="comparison-title-row">
          {scenario && <span className="pr-badge">{scenario.pr}</span>}
          <span className="comparison-name">
            {scenario ? scenario.title : manifest.app_name || manifest.filename}
          </span>
          {broken && <span className="badge badge-red">unparseable</span>}
        </div>

        <p className="comparison-claim">
          {scenario
            ? scenario.subtitle
            : "No walkthrough for this comparison — it can be run, but nothing here can narrate it."}
        </p>

        <div className="comparison-meta">
          {scenario && <div className="scenario-watch">{watchLine(scenario)}</div>}
          {!broken && !scenario && (
            <div className="comparison-dim">
              {manifest.workflow_count} workflow{manifest.workflow_count === 1 ? "" : "s"}
            </div>
          )}
        </div>

        {broken && (
          <div className="manifest-err">
            {manifest.filename}: {manifest.error}
          </div>
        )}
        {selected && (
          <div className="comparison-note">Picked for you · start it whenever you're ready</div>
        )}
      </div>

      <div className="comparison-actions">
        {scenario ? (
          <>
            <Link to={`/demo/${scenario.id}`} className="btn-pri act-link">
              Watch it happen
            </Link>
            {runnable && (
              <div className="run-real-wrap">
                <button className="run-real" onClick={() => onRun(manifest)}>
                  Run it for real
                </button>
                <div className="run-real-note">Docker required</div>
              </div>
            )}
          </>
        ) : runnable ? (
          <button className="btn-sec" onClick={() => onRun(manifest)}>
            Run it for real
          </button>
        ) : (
          // No walkthrough and nothing to run it with: say so rather than
          // leaving a card with no action and no explanation.
          <div className="run-real-note">{broken ? "Not runnable" : "Needs Docker"}</div>
        )}
      </div>
    </div>
  );
}

function ComparisonPicker({ manifests, error, selectedManifest, canRunReal, onRun }) {
  // Whatever goes wrong with the server, the walkthrough is unaffected —
  // it is bundled with the page. Every dead end offers it.
  const scriptedEscape = (
    <Link to="/demo/checkout-validation" className="btn-sec act-link">
      Watch a comparison instead
    </Link>
  );

  if (error) {
    return (
      <StateNotice variant="error" title="Could not reach the run server" detail={error}>
        {scriptedEscape}
      </StateNotice>
    );
  }

  if (manifests === null) {
    return <StateNotice variant="loading" title="Loading comparisons…" />;
  }

  if (manifests.length === 0) {
    return (
      <StateNotice
        variant="empty"
        title="No comparisons configured"
        detail={
          <>
            Drop a manifest into <code>demo/manifests/</code>, or point the server at
            another directory with <code>BEHAVIORDIFF_MANIFEST_DIR</code>.
          </>
        }
      >
        {scriptedEscape}
      </StateNotice>
    );
  }

  return (
    <>
      {/* Honesty, kept quiet: a walkthrough is a recorded run, not a
          simulation, and it is the path this page is built around. */}
      <div className="picker-note">
        A walkthrough replays a recorded run, real findings, real evidence.
        {canRunReal && " Running one live needs Docker and takes about a minute."}
      </div>

      <div className="sec-label">Comparisons</div>
      {manifests.map((m) => (
        <ComparisonCard
          key={m.path}
          manifest={m}
          selected={selectedManifest != null && m.filename === selectedManifest}
          canRunReal={canRunReal}
          onRun={onRun}
        />
      ))}
    </>
  );
}

export default function RunNew() {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();

  const [manifests, setManifests] = useState(null);
  const [listError, setListError] = useState(null);
  const [run, setRun] = useState(null);
  // The manifest picked but not yet started. A real run takes the better part
  // of a minute and opens on a progress list; this holds the orientation beat
  // in front of it so the wait means something. Nothing is POSTed until the
  // viewer asks for it from there.
  const [orienting, setOrienting] = useState(null);
  // Bumped when a run fails in a way that says the daemon is down, purely so
  // this component re-reads realRunsAvailable() and re-offers the scripted path.
  const [, setModeRevision] = useState(0);

  const socketRef = useRef(null);
  // Set when a done/error frame arrives, so onclose can tell "the run ended"
  // from "the socket dropped".
  const terminalRef = useRef(false);
  // Bumps per trigger, so a slow POST can't write into a later attempt.
  const attemptRef = useRef(0);
  const bottomRef = useRef(null);

  useEffect(() => {
    fetchManifests()
      .then(setManifests)
      .catch((e) => setListError(e.message));
  }, []);

  const closeSocket = useCallback(() => {
    const ws = socketRef.current;
    socketRef.current = null;
    if (ws) ws.close();
  }, []);

  useEffect(() => closeSocket, [closeSocket]);

  const openStream = useCallback((streamId) => {
    closeSocket();
    terminalRef.current = false;

    const ws = new WebSocket(runStreamUrl(streamId));
    socketRef.current = ws;

    ws.onopen = () => {
      if (socketRef.current !== ws) return;
      setRun((r) => (r ? { ...r, status: "streaming" } : r));
    };

    ws.onmessage = (frame) => {
      if (socketRef.current !== ws) return;

      let event;
      try {
        event = JSON.parse(frame.data);
      } catch {
        event = {
          stage: "unreadable-frame",
          message: String(frame.data).slice(0, 400),
          timestamp: Date.now() / 1000,
          data: null,
        };
      }

      const terminal = event.stage === "done" || event.stage === "error";
      if (terminal) {
        terminalRef.current = true;
        // Stop listening — the server closes after the sentinel anyway, but
        // the run is over as far as this page is concerned.
        socketRef.current = null;
        ws.close();
      }

      // Outside the state updater: updaters must stay pure under StrictMode.
      if (event.stage === "error") {
        const daemonDown = noteRunFailure({
          errorType: event.data?.error_type,
          message: event.data?.error || event.message,
        });
        if (daemonDown) setModeRevision((n) => n + 1);
      }

      setRun((r) => {
        if (!r) return r;
        const events = [...r.events, event];
        if (event.stage === "done") {
          return {
            ...r,
            events,
            status: "done",
            // The persisted run id, which is NOT the stream id — the registry
            // and the store mint their own. RunDetail wants the store's.
            resultRunId: event.data?.run_id ?? null,
          };
        }
        if (event.stage === "error") {
          return {
            ...r,
            events,
            status: "failed",
            error: event.data?.error || event.message || "the run failed",
            errorType: event.data?.error_type ?? null,
          };
        }
        return { ...r, events };
      });
    };

    ws.onclose = (closeEvent) => {
      if (socketRef.current !== ws) return;
      socketRef.current = null;
      if (closeEvent.code === WS_UNKNOWN_RUN) {
        setRun((r) => (r ? { ...r, status: "unknown-run" } : r));
        return;
      }
      if (terminalRef.current) return;
      setRun((r) => (r ? { ...r, status: "lost" } : r));
    };
  }, [closeSocket]);

  async function start(manifest) {
    const attempt = ++attemptRef.current;
    closeSocket();
    terminalRef.current = false;
    setOrienting(null);
    setRun({
      manifest,
      streamId: null,
      status: "connecting",
      events: [],
      error: null,
      errorType: null,
      resultRunId: null,
    });

    try {
      const { run_id: streamId } = await triggerRun(manifest.path);
      if (attempt !== attemptRef.current) return;
      setRun((r) => (r ? { ...r, streamId } : r));
      openStream(streamId);
    } catch (e) {
      if (attempt !== attemptRef.current) return;
      setRun((r) => (r ? { ...r, status: "failed", error: e.message } : r));
    }
  }

  function backToList() {
    attemptRef.current += 1;
    closeSocket();
    terminalRef.current = false;
    setRun(null);
    setOrienting(null);
  }

  // Follow the tail of the log as frames arrive.
  useEffect(() => {
    if (run?.events.length) bottomRef.current?.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }, [run?.events.length]);

  const canRunReal = realRunsAvailable();

  // Picked, not yet started: the same beat the demo flow opens on, so both
  // paths explain themselves the same way before anything starts moving.
  if (!run && orienting) {
    const scenario = scenarioForManifest(orienting.filename);
    return (
      <div className="demo-page">
        <DemoBackground diverged={false} />
        <button className="back-link stream-back" onClick={() => setOrienting(null)}>
          ← Back to scenarios
        </button>

        <OrientationPanel
          beat={beatFor(scenario)}
          onContinue={() => start(orienting)}
          continueLabel="Start the run →"
        />

        <p className="orient-foot">
          {scenario ? `Up next: ${scenario.title}. ` : ""}
          Both versions get built and started, so this takes about a minute.
        </p>
      </div>
    );
  }

  if (!run) {
    // Derived at render, never in an effect: the param is inert if the
    // manifest list fails to load, and it survives StrictMode's double pass.
    const preselected = searchParams.get("manifest");

    return (
      <div className="demo-page">
        <DemoBackground diverged={false} />
        <Link to="/" className="back-link">← Home</Link>

        <div className="demo-pane">
          <div className="pr-header">
            <div className="pr-row">
              <span className="pr-title">Pick a comparison</span>
            </div>
            <div className="pr-intent">
              Every one of these passed review and shipped green. Pick one and
              watch what it actually did.
            </div>
          </div>

          <ComparisonPicker
            manifests={manifests}
            error={listError}
            selectedManifest={preselected}
            canRunReal={canRunReal}
            onRun={setOrienting}
          />

          <div className="picker-foot">
            <Link to="/runs" className="back-link">All previous runs →</Link>
          </div>
        </div>
      </div>
    );
  }

  const { status, events, manifest } = run;
  const t0 = events.length ? events[0].timestamp : null;
  const live = IN_FLIGHT.has(status);
  const diverged = hasDiverged(events);
  const scenario = scenarioForManifest(manifest.filename);
  const phases = phaseStates(events, { live });

  const statusLine =
    (status === "connecting" && "Starting…") ||
    (status === "streaming" && "Running") ||
    (status === "done" && "Run complete") ||
    (status === "failed" && "Run failed") ||
    (status === "unknown-run" && "Run not found") ||
    (status === "lost" && "Stream disconnected");

  return (
    <div className="demo-page">
      <DemoBackground diverged={diverged} />
      <button className="back-link stream-back" onClick={backToList}>← Back to scenarios</button>

      <div className="demo-pane">
        <div className="pr-header">
          <div className="pr-row">
            {/* The change being compared, not the file it is configured in. */}
            <span className="pr-badge">{scenario ? scenario.pr : manifest.app_name || manifest.filename}</span>
            <span className="pr-title">{scenario ? scenario.title : statusLine}</span>
            {live && <span className="stream-pulse" />}
          </div>
          <div className="pr-intent">
            {scenario ? `${scenario.subtitle} · ${statusLine}` : statusLine}
          </div>
        </div>

        <PhaseList phases={phases} />

        <details className="raw-log">
          <summary className="raw-log-toggle">
            Raw log <span className="raw-log-count">{events.length}</span>
          </summary>
          <div className="stream-log-wrap">
            <ol className="log">
              {events.map((event, i) => {
                const last = i === events.length - 1;
                let state = "done";
                if (event.stage === "error") state = "err";
                else if (last && live) state = "active";
                return <LogRow key={i} event={event} state={state} t0={t0} />;
              })}
              {live && (
                <li className="log-row">
                  <div className="log-line">
                    <span className="log-dot log-dot-pending" />
                    <span className="log-time" />
                    <span className="log-msg log-waiting">
                      {status === "connecting" ? "connecting to the run stream…" : "waiting for the next stage…"}
                    </span>
                  </div>
                </li>
              )}
              <li ref={bottomRef} />
            </ol>
          </div>
        </details>

        {status === "failed" && (
          <div className="alert alert-err">
            <span className="alert-icon">⚠</span>
            <div>
              <div className="alert-title">
                The run failed{run.errorType ? ` · ${run.errorType}` : ""}
              </div>
              <div className="alert-detail">{run.error}</div>
            </div>
          </div>
        )}

        {status === "unknown-run" && (
          <div className="alert alert-err">
            <span className="alert-icon">⚠</span>
            <div>
              <div className="alert-title">This run could not be found</div>
              <div className="alert-detail">
                The server has no run with id <code>{run.streamId}</code>. It may have been evicted,
                or the server restarted after it was started.
              </div>
            </div>
          </div>
        )}

        {status === "lost" && (
          <div className="alert alert-warn">
            <span className="alert-icon">◌</span>
            <div>
              <div className="alert-title">The event stream disconnected</div>
              <div className="alert-detail">
                The run may still be going on the server. Reconnecting picks up new events —
                anything emitted while disconnected is not replayed.
              </div>
            </div>
          </div>
        )}

        <div className="stream-actions">
          {status === "done" && (
            <button
              className="btn-pri"
              disabled={!run.resultRunId}
              onClick={() => navigate(`/runs/${run.resultRunId}`)}
            >
              View full results
            </button>
          )}
          {status === "lost" && (
            <button
              className="btn-sec"
              onClick={() => {
                setRun((r) => (r ? { ...r, status: "connecting" } : r));
                openStream(run.streamId);
              }}
            >
              Reconnect
            </button>
          )}
          {(status === "failed" || status === "unknown-run" || status === "lost") && (
            <button className="btn-sec" onClick={backToList}>Try again</button>
          )}
          {(status === "done" || status === "lost") && (
            <Link to="/runs" className="btn-sec stream-link-btn">All runs</Link>
          )}
        </div>

        {status === "done" && !run.resultRunId && (
          <p className="stream-note">
            The run finished but reported no persisted run id, so there is nothing to open.
          </p>
        )}
      </div>
    </div>
  );
}
