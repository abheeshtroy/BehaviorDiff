"""Proxy observer: records outbound calls made by the app under test.

Runs a lightweight reverse-proxy HTTP server for one manifest outbound
service. Each incoming request is recorded as an OutboundCall and answered
with the mock response configured for that method/path in the manifest —
the app under test never reaches the real third-party service.

Recording and matching are kept separate from the socket server: record()
and diff() are plain methods over OutboundCall data, so they can be tested
without binding a port. The server itself (start/stop) exists for real runs
only.
"""

from __future__ import annotations

import json
import threading
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

import structlog
from pydantic import BaseModel, Field

from engine.manifest import MockResponse, OutboundService

log = structlog.get_logger(__name__)


class ProxyObserverError(Exception):
    """Raised when the proxy can't be started or a mock response can't be resolved."""


class OutboundCall(BaseModel):
    """A single outbound request recorded by the proxy."""

    method: str
    path: str
    headers: dict[str, str] = Field(default_factory=dict)
    body: Any = None


class OutboundCallDiff(BaseModel):
    """Recorded outbound calls, partitioned by whether both versions made them."""

    in_both: list[OutboundCall] = Field(default_factory=list)
    only_in_base: list[OutboundCall] = Field(default_factory=list)
    only_in_target: list[OutboundCall] = Field(default_factory=list)


def _decode_body(raw_body: bytes) -> Any:
    if not raw_body:
        return None
    try:
        return json.loads(raw_body)
    except ValueError:
        return raw_body.decode("utf-8", errors="replace")


def _call_signature(call: OutboundCall) -> tuple[str, str, str]:
    return (call.method.upper(), call.path, json.dumps(call.body, sort_keys=True, default=str))


class ProxyObserver:
    """Records outbound calls to one manifest OutboundService and mocks its responses."""

    def __init__(self, service: OutboundService, host: str = "0.0.0.0", port: int = 0) -> None:
        self.service = service
        self.host = host
        self.port = port
        self.calls: list[OutboundCall] = []
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def address(self) -> tuple[str, int]:
        if self._server is None:
            raise ProxyObserverError("proxy has not been started")
        return self._server.server_address[:2]  # type: ignore[return-value]

    def start(self) -> None:
        """Bind and start the proxy server in a background thread."""
        if self._server is not None:
            raise ProxyObserverError(f"proxy for service {self.service.name!r} is already running")
        handler = _make_handler(self)
        self._server = ThreadingHTTPServer((self.host, self.port), handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        log.info("proxy started", service=self.service.name, address=self._server.server_address)

    def stop(self) -> None:
        """Stop the proxy server. Safe to call more than once."""
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._server = None
        self._thread = None
        log.info("proxy stopped", service=self.service.name)

    def record(self, method: str, path: str, headers: dict[str, str], body: Any) -> OutboundCall:
        call = OutboundCall(method=method.upper(), path=path, headers=headers, body=body)
        with self._lock:
            self.calls.append(call)
        return call

    def mock_response_for(self, method: str, path: str) -> MockResponse:
        """Look up the manifest-configured mock response for a method/path.

        Query strings are stripped before matching, since mock_responses
        keys in the manifest are bare paths.
        """
        path_only = urlsplit(path).path
        key = f"{method.upper()} {path_only}"
        mock = self.service.mock_responses.get(key)
        if mock is None:
            raise ProxyObserverError(f"no mock_response configured for {key!r} on service {self.service.name!r}")
        return mock

    @staticmethod
    def diff(base_calls: list[OutboundCall], target_calls: list[OutboundCall]) -> OutboundCallDiff:
        """Compare calls recorded during a base run vs a target run.

        Pure function over already-recorded calls. Matches by (method, path,
        body) as a multiset: identical calls are paired one-to-one, so
        repeated calls made by only one version still show up as extras.
        """
        target_by_sig: dict[tuple[str, str, str], list[int]] = defaultdict(list)
        for idx, call in enumerate(target_calls):
            target_by_sig[_call_signature(call)].append(idx)

        matched_target_idxs: set[int] = set()
        in_both: list[OutboundCall] = []
        only_in_base: list[OutboundCall] = []
        for call in base_calls:
            candidates = target_by_sig.get(_call_signature(call), [])
            match = next((idx for idx in candidates if idx not in matched_target_idxs), None)
            if match is None:
                only_in_base.append(call)
            else:
                matched_target_idxs.add(match)
                in_both.append(call)

        only_in_target = [call for idx, call in enumerate(target_calls) if idx not in matched_target_idxs]

        return OutboundCallDiff(in_both=in_both, only_in_base=only_in_base, only_in_target=only_in_target)


def _make_handler(observer: ProxyObserver) -> type[BaseHTTPRequestHandler]:
    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            log.debug("proxy request", message=format % args)

        def _handle(self) -> None:
            length = int(self.headers.get("Content-Length", 0))
            raw_body = self.rfile.read(length) if length else b""
            body = _decode_body(raw_body)
            headers = dict(self.headers.items())

            observer.record(self.command, self.path, headers, body)

            try:
                mock = observer.mock_response_for(self.command, self.path)
            except ProxyObserverError as exc:
                payload = str(exc).encode("utf-8")
                self.send_response(502)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return

            payload = json.dumps(mock.body).encode("utf-8")
            self.send_response(mock.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        do_GET = _handle
        do_POST = _handle
        do_PUT = _handle
        do_PATCH = _handle
        do_DELETE = _handle

    return _Handler
