"""Tests for the signal-cli allowlist proxy security boundary.

These pin the fail-closed behavior of the proxy: what is allowed, what is
blocked, and that the allowlist is the hard ceiling. They exercise the pure
logic only (no network, no running server) so they run fast in CI.

The proxy file is named with a hyphen (``signal-allowlist-proxy.py``) so it is
loaded via importlib rather than a normal import.
"""

import importlib.util
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROXY_FILE = REPO_ROOT / "signal-allowlist-proxy.py"

ALLOWED = {"+15551234567", "+15559876543"}
NOT_ALLOWED = "+19998887777"


def _load_proxy():
    spec = importlib.util.spec_from_file_location("signal_allowlist_proxy", PROXY_FILE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def proxy(tmp_path):
    """A fresh proxy module with a known allowlist and empty UUID/account maps."""
    mod = _load_proxy()
    allowlist_file = tmp_path / "allowlist"
    allowlist_file.write_text("\n".join(sorted(ALLOWED)) + "\n", encoding="utf-8")
    mod.ALLOWLIST_PATH = str(allowlist_file)
    mod.reload_allowlist()
    with mod.UUID_CACHE_LOCK:
        mod.UUID_TO_NUMBER = {}
        mod.KNOWN_ACCOUNTS = set()
    return mod


def _check(proxy, req):
    # _check_request only reads module state, so build the handler without
    # running BaseHTTPRequestHandler.__init__ (which needs a live socket).
    handler = proxy.ProxyHandler.__new__(proxy.ProxyHandler)
    return handler._check_request(req)


def _send(**params):
    return {"jsonrpc": "2.0", "method": "send", "id": "1", "params": params}


# --- recipient allowlist -----------------------------------------------------


def test_allowlisted_recipient_allowed(proxy):
    ok, reason = _check(proxy, _send(recipient="+15551234567", message="hi"))
    assert ok, reason


def test_non_allowlisted_recipient_blocked(proxy):
    ok, reason = _check(proxy, _send(recipient=NOT_ALLOWED, message="hi"))
    assert not ok
    assert "allowlist" in reason


def test_recipient_list_all_must_be_allowed(proxy):
    ok, reason = _check(proxy, _send(recipient=["+15551234567", NOT_ALLOWED], message="hi"))
    assert not ok


def test_recipient_list_all_allowed(proxy):
    ok, reason = _check(proxy, _send(recipient=["+15551234567", "+15559876543"], message="hi"))
    assert ok, reason


def test_malformed_recipient_blocked(proxy):
    ok, reason = _check(proxy, _send(recipient=12345, message="hi"))
    assert not ok


# --- method allowlist (default-deny) ----------------------------------------


def test_disallowed_methods_blocked(proxy):
    for method in ("startLink", "finishLink", "updateGroup", "joinGroup", "trust", "unregister"):
        ok, reason = _check(proxy, {"jsonrpc": "2.0", "method": method, "id": "1"})
        assert not ok, f"{method} should be blocked"


def test_allowed_methods_set_is_minimal(proxy):
    # Pin the boundary: adding a method is a deliberate, reviewed change.
    assert proxy.ALLOWED_METHODS == {
        "listAccounts",
        "listContacts",
        "send",
        "sendTyping",
        "sendReceipt",
        "sendReaction",
        "remoteDelete",
        "getAttachment",
    }


# --- addressing restrictions ------------------------------------------------


def test_group_addressing_blocked(proxy):
    ok, reason = _check(proxy, _send(groupId="abc", message="hi"))
    assert not ok
    assert "group" in reason


def test_username_addressing_blocked(proxy):
    ok, reason = _check(proxy, _send(username="someone", message="hi"))
    assert not ok
    assert "username" in reason


def test_note_to_self_allowed(proxy):
    ok, reason = _check(proxy, _send(noteToSelf=True, message="note"))
    assert ok, reason


# --- account restriction -----------------------------------------------------


def test_unknown_account_blocked(proxy):
    ok, reason = _check(
        proxy, _send(account="+10000000000", recipient="+15551234567", message="hi")
    )
    assert not ok
    assert "account" in reason


def test_known_account_allowed(proxy):
    with proxy.UUID_CACHE_LOCK:
        proxy.KNOWN_ACCOUNTS = {"+15551234567"}
    ok, reason = _check(
        proxy, _send(account="+15551234567", recipient="+15551234567", message="hi")
    )
    assert ok, reason


# --- UUID recipient resolution ----------------------------------------------


def test_uuid_resolved_to_allowlisted_allowed(proxy):
    with proxy.UUID_CACHE_LOCK:
        proxy.UUID_TO_NUMBER = {"uuid-1": "+15551234567"}
    ok, reason = _check(proxy, _send(recipient="uuid-1", message="hi"))
    assert ok, reason


def test_uuid_resolved_to_non_allowlisted_blocked(proxy):
    with proxy.UUID_CACHE_LOCK:
        proxy.UUID_TO_NUMBER = {"uuid-1": NOT_ALLOWED}
    ok, reason = _check(proxy, _send(recipient="uuid-1", message="hi"))
    assert not ok


# --- allowlist file parsing --------------------------------------------------


def test_load_allowlist_file_ignores_invalid(tmp_path):
    mod = _load_proxy()
    f = tmp_path / "al"
    f.write_text(
        "# comment\n+15551234567\n\n+12345\nnot-a-number\n+15559876543\n", encoding="utf-8"
    )
    assert mod.load_allowlist_file(str(f)) == {"+15551234567", "+15559876543"}


# --- incoming event filtering ------------------------------------------------


def test_extract_sender_direct(proxy):
    data = json.dumps({"envelope": {"sourceNumber": "+15551234567"}})
    sender, is_group = proxy._extract_sender(data)
    assert sender == "+15551234567"
    assert is_group is False


def test_extract_sender_group(proxy):
    data = json.dumps({"envelope": {"sourceNumber": "+15551234567", "group": {"id": "g1"}}})
    sender, is_group = proxy._extract_sender(data)
    assert sender == "+15551234567"
    assert is_group is True


def test_extract_sender_bad_json(proxy):
    sender, is_group = proxy._extract_sender("not json")
    assert sender == ""
    assert is_group is False
