import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { fetchManifests, runStreamUrl, triggerRun } from "../api";

// The stream is a real log, not a fixed sequence: the pipeline yields one
// workflow_started/workflow_completed pair per workflow, skips stages that
// don't apply, and grows new ones over time. So nothing here enumerates
// stages — every frame that arrives gets a row, in arrival order.

// Matches WS_UNKNOWN_RUN in web/api.py: the run id isn't registered.
const WS_UNKNOWN_RUN = 4004;

// status: connecting → streaming → done | failed, or unknown-run / lost.
const IN_FLIGHT = new Set(["connecting", "streaming"]);

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

function ManifestList({ manifests, error, onPick }) {
  if (error) {
    return (
      <div className="alert alert-err">
        <span className="alert-icon">⚠</span>
        <div>
          <div className="alert-title">Could not load manifests</div>
          <div className="alert-detail">{error}</div>
        </div>
      </div>
    );
  }

  if (manifests === null) {
    return <p style={{ color: "var(--text-3)", padding: "40px 0" }}>Loading manifests…</p>;
  }

  if (manifests.length === 0) {
    return (
      <div className="empty-state">
        <div className="empty-icon">◇</div>
        <p className="empty-title">No manifests found</p>
        <p className="empty-hint">
          Drop a manifest into <code>demo/manifests/</code>, or point the server at another
          directory with <code>BEHAVIORDIFF_MANIFEST_DIR</code>.
        </p>
      </div>
    );
  }

  return (
    <>
      <div className="sec-label">Manifests</div>
      {manifests.map((m) => (
        <div key={m.path} className="manifest-card" onClick={() => onPick(m)}>
          <div className="manifest-left">
            <div className="manifest-title-row">
              <span className="manifest-app">{m.app_name || m.filename}</span>
              {m.error ? (
                <span className="badge badge-red">unparseable</span>
              ) : (
                <span className="badge badge-muted">
                  {m.workflow_count} workflow{m.workflow_count === 1 ? "" : "s"}
                </span>
              )}
            </div>
            <div className="manifest-path">{m.path}</div>
            {m.error && <div className="manifest-err">{m.error}</div>}
          </div>
          <span className="manifest-go">run →</span>
        </div>
      ))}
    </>
  );
}

export default function RunNew() {
  const navigate = useNavigate();

  const [manifests, setManifests] = useState(null);
  const [listError, setListError] = useState(null);
  const [run, setRun] = useState(null);

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
  }

  // Follow the tail of the log as frames arrive.
  useEffect(() => {
    if (run?.events.length) bottomRef.current?.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }, [run?.events.length]);

  if (!run) {
    return (
      <div style={{ paddingTop: "24px" }}>
        <Link to="/runs" className="back-link">← All runs</Link>
        <div className="pr-header">
          <div className="pr-row">
            <span className="pr-title">Run a comparison</span>
          </div>
          <div className="pr-intent">
            Pick a manifest. Both versions are built and started, the workflows run against
            each, and the observations are compared.
          </div>
        </div>
        <ManifestList manifests={manifests} error={listError} onPick={start} />
      </div>
    );
  }

  const { status, events, manifest } = run;
  const t0 = events.length ? events[0].timestamp : null;
  const live = IN_FLIGHT.has(status);

  return (
    <div style={{ paddingTop: "24px" }}>
      <button className="back-link stream-back" onClick={backToList}>← Manifests</button>

      <div className="pr-header">
        <div className="pr-row">
          <span className="pr-badge">{manifest.app_name || manifest.filename}</span>
          <span className="pr-title">
            {status === "connecting" && "Starting…"}
            {status === "streaming" && "Running"}
            {status === "done" && "Run complete"}
            {status === "failed" && "Run failed"}
            {status === "unknown-run" && "Run not found"}
            {status === "lost" && "Stream disconnected"}
          </span>
          {live && <span className="stream-pulse" />}
        </div>
        <div className="pr-intent">{manifest.path}</div>
      </div>

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
  );
}
