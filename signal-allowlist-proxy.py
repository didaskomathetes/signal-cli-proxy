#!/usr/bin/env python3
"""
Signal CLI Allowlist Proxy
==========================

A filtering, drop-in proxy in front of signal-cli's HTTP JSON-RPC API.

It sits between the (untrusted) Hermes agent and signal-cli and enforces an
allowlist in BOTH directions, so the agent can never widen the allowlist:

  * OUTGOING: only JSON-RPC methods in ``ALLOWED_METHODS`` are forwarded
    (default-deny). Any call that carries a ``recipient`` is only forwarded
    if every recipient is in the allowlist. Group and username addressing
    is rejected.
  * INCOMING: the proxy keeps a single SSE connection to signal-cli's
    ``/api/v1/events`` and only forwards ``receive`` events whose sender is
    in the allowlist (group events are dropped) to the agent.

Because the proxy is the only path to signal-cli, the agent's own allowlist
can only ever be a SUBSET of the proxy's allowlist, never a superset. This
matters because the agent may be compromised (e.g. prompt injection); it must
not be able to receive messages from, or send messages to, anyone other than
the numbers in this proxy's allowlist.

Endpoints (drop-in compatible with signal-cli's HTTP API):
  GET  /api/v1/check   -> 200 (health)
  POST /api/v1/rpc     -> JSON-RPC (default-deny method allowlist)
  GET  /api/v1/events  -> SSE stream of allowlisted incoming messages
  GET  /health         -> proxy status JSON
"""

import argparse
import contextlib
import http.client
import json
import logging
import queue
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("signal-allowlist-proxy")

# E.164 phone number
PHONE_PATTERN = re.compile(r"^\+\d{10,15}$")

# Max size of an RPC request body. Attachments are base64 data URIs (~33%
# larger than the raw file), so this must fit photos and voice notes;
# anything bigger is rejected (413).
MAX_BODY_BYTES = 50 * 1024 * 1024

# ---------------------------------------------------------------------------
# Method allowlist (default-deny)
#
# signal-cli's JSON-RPC API exposes every CLI command as a method, including
# dangerous ones (startLink/finishLink, updateGroup, joinGroup, trust,
# unregister, ...). The proxy only forwards the methods in ALLOWED_METHODS;
# everything else is rejected with 403. This is the hard boundary: even a
# fully compromised agent cannot link new devices, create or modify groups,
# or touch account settings.
#
# Methods that address a recipient additionally require every recipient to be
# in the allowlist.
# ---------------------------------------------------------------------------
ALLOWED_METHODS = {
    "listAccounts",
    "listContacts",
    "send",
    "sendTyping",
    "sendReceipt",
    "sendReaction",
    "remoteDelete",
    "getAttachment",
}

# ---------------------------------------------------------------------------
# Configuration (filled in main())
# ---------------------------------------------------------------------------
ALLOWLIST_PATH = None
SIGNAL_CLI_URL = None
UPSTREAM_HOST = None
UPSTREAM_PORT = None
RELOAD_INTERVAL = 30

# The current allowlist. Reassigned atomically by reload_allowlist(); readers
# always see a consistent set because the reference is swapped, not mutated.
ALLOWLIST = set()

# Whether the upstream SSE connection is currently up (for /health).
UPSTREAM_CONNECTED = False


def load_allowlist_file(path):
    """Read the allowlist file and return a set of E.164 numbers."""
    allowlist = set()
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if PHONE_PATTERN.match(line):
                allowlist.add(line)
            else:
                logger.warning("Ignoring invalid allowlist entry: %r", line)
    return allowlist


def reload_allowlist():
    global ALLOWLIST
    try:
        new = load_allowlist_file(ALLOWLIST_PATH)
        if new != ALLOWLIST:
            logger.info("Allowlist reloaded: %d entries", len(new))
        ALLOWLIST = new
    except Exception as e:  # keep serving with the last good list
        logger.error("Failed to reload allowlist: %s", e)


def reload_loop():
    while True:
        time.sleep(RELOAD_INTERVAL)
        reload_allowlist()


# ---------------------------------------------------------------------------
# Recipient resolution (UUID -> E.164 number)
#
# signal-cli (and the Hermes agent) increasingly address recipients by their
# stable ACI/PNI UUID rather than by phone number. The allowlist is a set of
# E.164 numbers, so before checking a recipient we resolve any UUID to its
# phone number via signal-cli's contact list. Anything that cannot be resolved
# to an allowlisted number is blocked (fail closed).
# ---------------------------------------------------------------------------
UUID_TO_NUMBER = {}
KNOWN_ACCOUNTS = set()
UUID_CACHE_LOCK = threading.Lock()  # guards reads/swaps of the maps below
UUID_REFRESH_LOCK = threading.Lock()  # serializes map refreshes


def _rpc_call(method, params):
    """Make a JSON-RPC call to signal-cli and return its ``result`` (or raise)."""
    payload = json.dumps(
        {"jsonrpc": "2.0", "method": method, "id": "proxy-internal", "params": params}
    ).encode("utf-8")
    conn = http.client.HTTPConnection(UPSTREAM_HOST, UPSTREAM_PORT, timeout=30)
    try:
        conn.request(
            "POST", "/api/v1/rpc", body=payload, headers={"Content-Type": "application/json"}
        )
        resp = conn.getresponse()
        data = resp.read()
        if resp.status != 200:
            raise OSError(f"signal-cli RPC {method} returned status {resp.status}")
        parsed = json.loads(data)
        if isinstance(parsed, dict) and "error" in parsed:
            raise OSError("signal-cli RPC {} error: {}".format(method, parsed["error"]))
        return parsed.get("result") if isinstance(parsed, dict) else parsed
    finally:
        conn.close()


def _refresh_uuid_map():
    """Rebuild the UUID->number map and known-account set from signal-cli."""
    global UUID_TO_NUMBER, KNOWN_ACCOUNTS
    with UUID_REFRESH_LOCK:
        try:
            accounts = _rpc_call("listAccounts", {})
            new_map = {}
            new_accounts = set()
            if isinstance(accounts, list):
                for acc in accounts:
                    number = acc.get("number") if isinstance(acc, dict) else None
                    if not number:
                        continue
                    new_accounts.add(number)
                    contacts = _rpc_call("listContacts", {"account": number, "allRecipients": True})
                    if isinstance(contacts, list):
                        for c in contacts:
                            if not isinstance(c, dict):
                                continue
                            num = c.get("number")
                            uid = c.get("uuid")
                            if num and uid:
                                new_map[uid] = num
            # Swap both maps atomically under the cache lock.
            with UUID_CACHE_LOCK:
                UUID_TO_NUMBER = new_map
                KNOWN_ACCOUNTS = new_accounts
            logger.info(
                "UUID map refreshed: %d entries, %d account(s)", len(new_map), len(new_accounts)
            )
        except Exception as e:
            logger.error("Failed to refresh UUID map: %s", e)


def uuid_refresh_loop():
    """Periodically refresh the UUID map and known-account set.

    The initial refresh can fail when the proxy starts before signal-cli's
    HTTP API is ready (boot/healthcheck race). Without retries,
    KNOWN_ACCOUNTS would stay empty and every RPC carrying an ``account``
    param would be blocked (fail closed) until a manual proxy restart.
    """
    while True:
        try:
            _refresh_uuid_map()
        except Exception:
            logger.exception("Unexpected error refreshing UUID map; continuing loop")
        time.sleep(RELOAD_INTERVAL)


def resolve_recipient(r):
    """Resolve a recipient (E.164 number or UUID) to a canonical E.164 number.

    Returns the number, or ``None`` if it cannot be resolved.
    """
    if not r:
        return None
    if PHONE_PATTERN.match(r):
        return r
    # Not a phone number: treat as a UUID and resolve via the contact map.
    with UUID_CACHE_LOCK:
        cached = UUID_TO_NUMBER.get(r)
    if cached:
        return cached
    _refresh_uuid_map()
    with UUID_CACHE_LOCK:
        return UUID_TO_NUMBER.get(r)


# ---------------------------------------------------------------------------
# Upstream SSE consumer + broadcaster
# ---------------------------------------------------------------------------
class Broadcaster:
    """Fan-out of filtered SSE events to connected agent clients."""

    def __init__(self):
        self._lock = threading.Lock()
        self._clients = {}

    def register(self):
        q = queue.Queue(maxsize=1000)
        cid = id(q)
        with self._lock:
            self._clients[cid] = q
        return cid, q

    def unregister(self, cid):
        with self._lock:
            self._clients.pop(cid, None)

    def broadcast(self, raw_event):
        with self._lock:
            clients = list(self._clients.values())
        for q in clients:
            try:
                q.put_nowait(raw_event)
            except queue.Full:
                logger.warning("Dropping event for slow client (queue full)")

    @property
    def client_count(self):
        with self._lock:
            return len(self._clients)


BROADCASTER = Broadcaster()


def _extract_sender(data_str):
    """Return (sender, is_group) from a `receive` event payload.

    ``sender`` is '' if it cannot be determined. ``is_group`` is True when the
    envelope belongs to a group (group events are always dropped).
    """
    try:
        msg = json.loads(data_str)
    except json.JSONDecodeError:
        return "", False
    envelope = msg.get("envelope", {}) or {}
    sender = envelope.get("sourceNumber") or envelope.get("source") or ""
    is_group = bool(envelope.get("group"))
    return sender, is_group


def _on_sse_event(event_name, data_str, raw_block):
    """Called for each complete SSE event from the upstream."""
    if event_name != "receive":
        return
    sender, is_group = _extract_sender(data_str)
    if is_group:
        logger.warning("Dropped incoming group message (groups are not allowed)")
        return
    if sender in ALLOWLIST:
        BROADCASTER.broadcast(raw_block)
    else:
        logger.warning("Dropped incoming message from non-allowed sender: %s", sender)


def _parse_sse_stream(resp, on_event):
    """Parse an SSE stream (http.client response) and invoke on_event per event.

    signal-cli writes fields without a space after the colon, e.g.
        event:receive
        data:{...}
        <blank line>
    and keep-alives as a lone comment line ``:``.
    """
    event_name = None
    data_lines = []
    raw = []
    for raw_line in resp:
        raw.append(raw_line)
        line = raw_line.decode("utf-8", "replace").rstrip("\r\n")
        if line == "":
            # End of event
            if event_name is not None and data_lines:
                on_event(event_name, "".join(data_lines), b"".join(raw))
            event_name = None
            data_lines = []
            raw = []
        elif line.startswith(":"):
            # Comment / keep-alive; ignore
            continue
        elif line.startswith("event:"):
            event_name = line[len("event:") :].strip()
        elif line.startswith("data:"):
            d = line[len("data:") :]
            if d.startswith(" "):
                d = d[1:]
            data_lines.append(d)
        # other fields (id:, retry:) are ignored


# signal-cli's daemon can silently stop delivering events to long-lived SSE
# clients while keeping the TCP connection open (observed 2026-09-07: a
# ~10h-old connection stopped receiving events while fresh clients worked,
# and the proxy's upstream_connected flag stayed true, so nothing reconnected
# and every inbound message was dropped). Reconnect the upstream periodically
# so a stale connection can never outlive this window.
UPSTREAM_MAX_AGE_SECONDS = 6 * 60 * 60


def upstream_loop():
    """Continuously read signal-cli's /api/v1/events and broadcast filtered events."""
    global UPSTREAM_CONNECTED
    while True:
        conn = None
        reaper = None
        try:
            conn = http.client.HTTPConnection(UPSTREAM_HOST, UPSTREAM_PORT, timeout=30)
            conn.request("GET", "/api/v1/events")
            resp = conn.getresponse()
            if resp.status != 200:
                raise OSError(f"upstream /api/v1/events returned status {resp.status}")
            UPSTREAM_CONNECTED = True
            logger.info("Connected to upstream SSE at %s:%s", UPSTREAM_HOST, UPSTREAM_PORT)

            def _reap(_conn=conn):
                logger.info(
                    "Upstream SSE reached max age (%ds); forcing reconnect",
                    UPSTREAM_MAX_AGE_SECONDS,
                )
                with contextlib.suppress(Exception):
                    _conn.close()

            reaper = threading.Timer(UPSTREAM_MAX_AGE_SECONDS, _reap)
            reaper.daemon = True
            reaper.start()
            try:
                _parse_sse_stream(resp, _on_sse_event)
            finally:
                reaper.cancel()
        except Exception as e:
            logger.warning("Upstream SSE error: %s; reconnecting in 5s", e)
        finally:
            UPSTREAM_CONNECTED = False
            if conn is not None:
                with contextlib.suppress(Exception):
                    conn.close()
            time.sleep(5)


# ---------------------------------------------------------------------------
# HTTP request handler (drop-in for signal-cli's HTTP API)
# ---------------------------------------------------------------------------
class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "SignalAllowlistProxy/1.0"

    def log_message(self, fmt, *args):
        logger.info("%s %s", self.address_string(), fmt % args)

    # -- routing -----------------------------------------------------------
    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/v1/check":
            self._send_empty(200)
        elif path == "/api/v1/events":
            self._handle_events()
        elif path == "/health":
            self._send_json(
                200,
                {
                    "status": "ok",
                    "allowlist_size": len(ALLOWLIST),
                    "clients": BROADCASTER.client_count,
                    "upstream_connected": UPSTREAM_CONNECTED,
                },
            )
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/v1/rpc":
            self._handle_rpc()
        else:
            self._send_json(404, {"error": "not found"})

    # -- helpers -----------------------------------------------------------
    def _read_body(self):
        """Read the request body. Returns (body, error_response_sent)."""
        raw_len = self.headers.get("Content-Length")
        try:
            length = int(raw_len) if raw_len is not None else 0
        except ValueError:
            # Body (if any) is unreadable; do not reuse this connection.
            self.close_connection = True
            self._send_json(400, {"error": "invalid Content-Length"})
            return None, True
        if length < 0 or length > MAX_BODY_BYTES:
            # Body is not read; do not reuse this connection.
            self.close_connection = True
            self._send_json(413, {"error": "request body too large"})
            return None, True
        return self.rfile.read(length) if length else b"", False

    def _send_empty(self, status):
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _send_json(self, status, obj):
        data = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # -- JSON-RPC (outgoing) ----------------------------------------------
    def _check_request(self, req):
        """Validate one JSON-RPC request. Returns (ok, reason). Fail closed."""
        if not isinstance(req, dict):
            return False, "malformed request"
        method = req.get("method")
        if not isinstance(method, str) or method not in ALLOWED_METHODS:
            return False, f"method not allowed: {method}"
        params = req.get("params")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            return False, "malformed params"
        # Group and username addressing can reach people outside the allowlist.
        if "groupId" in params:
            return False, "group operations are not allowed"
        if "username" in params:
            return False, "username addressing is not allowed"
        # Notes-to-self only reach the account itself.
        if params.get("noteToSelf") is True:
            return True, None
        # In multi-account mode the agent must not be able to operate on an
        # account it does not know about.
        account = params.get("account")
        if account is not None:
            with UUID_CACHE_LOCK:
                known = set(KNOWN_ACCOUNTS)
            if not known or account not in known:
                return False, f"unknown account: {account}"
        recipient = params.get("recipient")
        if recipient is None:
            return True, None
        if isinstance(recipient, str):
            recipients = [recipient]
        elif isinstance(recipient, list):
            recipients = recipient
        else:
            return False, "malformed recipient"
        for r in recipients:
            if not isinstance(r, str) or not r:
                return False, "malformed recipient"
            resolved = resolve_recipient(r)
            if resolved is None or resolved not in ALLOWLIST:
                return False, f"recipient not in allowlist: {r}"
        return True, None

    def _handle_rpc(self):
        body, err_sent = self._read_body()
        if err_sent:
            return
        try:
            rpc = json.loads(body)
        except json.JSONDecodeError:
            self._send_json(400, {"error": "invalid json"})
            return

        # Support both single requests and JSON-RPC batches (fail closed).
        requests = rpc if isinstance(rpc, list) else [rpc]
        for req in requests:
            ok, reason = self._check_request(req)
            if not ok:
                method = req.get("method", "?") if isinstance(req, dict) else "?"
                logger.warning("Blocked %s: %s", method, reason)
                err = {
                    "jsonrpc": "2.0",
                    "error": {"code": -32000, "message": f"Access denied: {reason}"},
                    "id": req.get("id") if isinstance(req, dict) else None,
                }
                self._send_json(403, err if not isinstance(rpc, list) else [err])
                return

        self._forward_rpc(body)

    def _forward_rpc(self, body):
        try:
            conn = http.client.HTTPConnection(UPSTREAM_HOST, UPSTREAM_PORT, timeout=60)
            try:
                conn.request(
                    "POST", "/api/v1/rpc", body=body, headers={"Content-Type": "application/json"}
                )
                resp = conn.getresponse()
                data = resp.read()
                self.send_response(resp.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                if data:
                    self.wfile.write(data)
            finally:
                conn.close()
        except Exception as e:
            logger.error("Failed to forward RPC to signal-cli: %s", e)
            with contextlib.suppress(Exception):
                self._send_json(502, {"error": "signal-cli unreachable"})

    # -- SSE events (incoming) --------------------------------------------
    def _handle_events(self):
        cid, q = BROADCASTER.register()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            self.wfile.write(b"retry:5000\n\n")
            self.wfile.flush()
            logger.info("Agent SSE client connected (%d total)", BROADCASTER.client_count)
            last_keepalive = time.time()
            while True:
                try:
                    data = q.get(timeout=5)
                    self.wfile.write(data)
                    self.wfile.flush()
                except queue.Empty:
                    now = time.time()
                    if now - last_keepalive >= 15:
                        self.wfile.write(b":\n")
                        self.wfile.flush()
                        last_keepalive = now
                except (BrokenPipeError, ConnectionResetError, OSError):
                    break
        finally:
            BROADCASTER.unregister(cid)
            logger.info("Agent SSE client disconnected (%d total)", BROADCASTER.client_count)


# ---------------------------------------------------------------------------
def main():
    global ALLOWLIST_PATH, SIGNAL_CLI_URL, UPSTREAM_HOST, UPSTREAM_PORT, RELOAD_INTERVAL

    parser = argparse.ArgumentParser(description="Signal CLI Allowlist Proxy")
    parser.add_argument("--allowlist", required=True, help="Path to the allowlist file")
    parser.add_argument(
        "--signal-cli", default="http://signal-cli:9920", help="Base URL of the signal-cli HTTP API"
    )
    parser.add_argument("--port", type=int, default=9921, help="Port to listen on")
    parser.add_argument(
        "--reload-interval", type=int, default=30, help="Seconds between allowlist reloads"
    )
    args = parser.parse_args()

    ALLOWLIST_PATH = args.allowlist
    SIGNAL_CLI_URL = args.signal_cli
    RELOAD_INTERVAL = args.reload_interval

    parsed = urlparse(SIGNAL_CLI_URL)
    UPSTREAM_HOST = parsed.hostname or "localhost"
    UPSTREAM_PORT = parsed.port or 9920

    reload_allowlist()
    logger.info(
        "Allowlist proxy listening on :%d -> %s (allowlist: %d entries)",
        args.port,
        SIGNAL_CLI_URL,
        len(ALLOWLIST),
    )

    threading.Thread(target=reload_loop, daemon=True).start()
    threading.Thread(target=upstream_loop, daemon=True).start()
    threading.Thread(target=uuid_refresh_loop, daemon=True).start()

    server = ThreadingHTTPServer(("0.0.0.0", args.port), ProxyHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
