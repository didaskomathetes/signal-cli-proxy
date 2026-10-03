"""Tests for the signal-cli watchdog (watchdog.sh).

The watchdog's ``probe`` is the decision to KILL the daemon, so its
success/failure logic and its request shape are pinned here. The functions are
loaded by *sourcing* watchdog.sh in a bash subprocess — its main loop is
guarded (``BASH_SOURCE`` check) so sourcing defines the constants and functions
without starting the infinite loop.
"""

import json
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
WATCHDOG = REPO_ROOT / "watchdog.sh"

pytestmark = pytest.mark.skipif(shutil.which("curl") is None, reason="curl is required")


class _MockRpc:
    """A tiny JSON-RPC mock: records request bodies, returns a fixed reply."""

    def __init__(self, status=200, body='{"jsonrpc":"2.0","id":1,"result":[]}'):
        self.status = status
        self.body = body
        self.request_bodies = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                outer.request_bodies.append(self.rfile.read(length).decode("utf-8"))
                payload = outer.body.encode("utf-8")
                self.send_response(outer.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}/api/v1/rpc"

    def close(self):
        self._server.shutdown()
        self._server.server_close()


def _probe(url):
    """Source the watchdog, point RPC_URL at ``url``, run probe once.

    Returns (ok: bool, stdout: str).
    """
    script = (
        f"source {WATCHDOG}\n"
        f'RPC_URL="{url}"\n'
        "if probe; then echo PROBE_OK; else echo PROBE_FAIL; fi\n"
    )
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
    return "PROBE_OK" in proc.stdout, proc.stdout


def test_probe_succeeds_on_healthy_result():
    mock = _MockRpc(status=200, body='{"jsonrpc":"2.0","id":1,"result":[]}')
    try:
        ok, _ = _probe(mock.url)
        assert ok is True
    finally:
        mock.close()


def test_probe_request_body_matches_proxy_shape():
    # Fix: the probe must send an explicit (empty) "params" object, matching the
    # proxy's _rpc_call request shape. A param-less probe would false-negative
    # if signal-cli ever required the field, and the probe is the kill decision.
    mock = _MockRpc(status=200, body='{"jsonrpc":"2.0","id":1,"result":[]}')
    try:
        _probe(mock.url)
        assert len(mock.request_bodies) == 1
        body = json.loads(mock.request_bodies[0])
        assert body["method"] == "listAccounts"
        assert "params" in body, "probe must include a 'params' field"
        assert body["params"] == {}
    finally:
        mock.close()


def test_probe_fails_on_http_500():
    mock = _MockRpc(status=500, body='{"jsonrpc":"2.0","id":1,"result":[]}')
    try:
        ok, _ = _probe(mock.url)
        assert ok is False
    finally:
        mock.close()


def test_probe_fails_on_jsonrpc_error_no_result():
    # A 200 that carries a JSON-RPC error (no "result" field) is a failure:
    # the daemon answered, but not successfully.
    mock = _MockRpc(
        status=200,
        body='{"jsonrpc":"2.0","id":1,"error":{"code":-32601,"message":"not found"}}',
    )
    try:
        ok, _ = _probe(mock.url)
        assert ok is False
    finally:
        mock.close()


def test_probe_fails_on_200_without_result_key():
    # A 200 body that merely contains the word "result" (no "result": key) must
    # not pass — the grep requires the colon after the key.
    mock = _MockRpc(status=200, body='{"jsonrpc":"2.0","id":1,"error":"no result here"}')
    try:
        ok, _ = _probe(mock.url)
        assert ok is False
    finally:
        mock.close()


def test_probe_fails_on_connection_refused():
    # Point at a closed port: curl fails, probe must report failure.
    ok, _ = _probe("http://127.0.0.1:1/api/v1/rpc")
    assert ok is False
