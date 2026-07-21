"""Tests for MCP stdio protocol: JSON-RPC shape and errors vs CallToolResult."""

from __future__ import annotations

from unittest import mock

import pytest

import src.mcp.protocol as _prot


@pytest.fixture(autouse=True)
def _reset_protocol_globals():
    """Hermetic module-global state: an `initialize` here must not leak the
    elicitation capability into other test files (it made every later
    mutating-reflex test attempt a real stdin round-trip)."""
    _prot._client_capabilities.clear()
    _prot._pending_lines.clear()
    yield
    _prot._client_capabilities.clear()
    _prot._pending_lines.clear()


def test_tools_call_unknown_tool_returns_json_rpc_error():
    from src.mcp.protocol import handle_request

    msg = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "nonexistent_tool_xyz", "arguments": {}},
    }
    resp = handle_request(msg)
    assert resp is not None
    assert resp["id"] == 1
    assert "error" in resp
    assert resp["error"]["code"] == -32601


def test_tools_call_handler_exception_returns_call_tool_result_is_error():
    """MCP expects tool failures as result content + isError, not JSON-RPC error."""

    import src.mcp.protocol as prot

    def fail_handler(_args: object) -> str:
        raise RuntimeError("simulated boom")

    with mock.patch.dict(prot.TOOL_HANDLERS, {"fake_tool_that_fails": fail_handler}, clear=False):
        msg = {
            "jsonrpc": "2.0",
            "id": 42,
            "method": "tools/call",
            "params": {"name": "fake_tool_that_fails", "arguments": {}},
        }
        resp = prot.handle_request(msg)

    assert resp is not None
    assert resp["id"] == 42
    assert "error" not in resp
    result = resp["result"]
    assert result.get("isError") is True
    content = result["content"]
    assert len(content) == 1
    assert content[0]["type"] == "text"
    assert "simulated boom" in content[0]["text"]


def test_tools_call_success_returns_plain_call_tool_result():
    import src.mcp.protocol as prot

    def ok_handler(_args: object) -> str:
        return "ok"

    with mock.patch.dict(prot.TOOL_HANDLERS, {"fake_tool_ok": ok_handler}, clear=False):
        resp = prot.handle_request(
            {
                "jsonrpc": "2.0",
                "id": "req-99",
                "method": "tools/call",
                "params": {"name": "fake_tool_ok", "arguments": {}},
            }
        )

    assert resp["result"]["content"][0]["text"] == "ok"
    assert resp["result"].get("isError") not in (True,)


def _initialize(prot, capabilities):
    resp = prot.handle_request({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"capabilities": capabilities},
    })
    assert resp is not None and "result" in resp


def test_initialize_records_client_capabilities():
    import src.mcp.protocol as prot

    _initialize(prot, {"elicitation": {}})
    assert prot.client_supports_elicitation() is True

    _initialize(prot, {})
    assert prot.client_supports_elicitation() is False


def test_elicit_confirmation_returns_none_without_capability():
    import src.mcp.protocol as prot

    _initialize(prot, {})
    assert prot.elicit_confirmation("Proceed?") is None


def _run_elicitation(monkeypatch, client_response_factory):
    """Drive elicit_confirmation with a fake stdio pair; returns (result, request)."""
    import io
    import json as _json
    import sys as _sys

    import src.mcp.protocol as prot

    _initialize(prot, {"elicitation": {}})

    out = io.StringIO()
    monkeypatch.setattr(_sys, "stdout", out)

    state = {}

    def fake_readline():
        if "reply" not in state:
            request = _json.loads(out.getvalue().strip().splitlines()[-1])
            state["request"] = request
            state["reply"] = _json.dumps(client_response_factory(request)) + "\n"
            return state["reply"]
        return ""  # EOF on any further read

    monkeypatch.setattr(_sys, "stdin", mock.Mock(readline=fake_readline))
    result = prot.elicit_confirmation("Delete 12 items?")
    return result, state.get("request")


def test_elicit_confirmation_accept(monkeypatch):
    result, request = _run_elicitation(
        monkeypatch,
        lambda req: {
            "jsonrpc": "2.0",
            "id": req["id"],
            "result": {"action": "accept", "content": {"confirm": True}},
        },
    )
    assert result is True
    assert request["method"] == "elicitation/create"
    assert request["params"]["message"] == "Delete 12 items?"


def test_elicit_confirmation_decline(monkeypatch):
    result, _ = _run_elicitation(
        monkeypatch,
        lambda req: {
            "jsonrpc": "2.0",
            "id": req["id"],
            "result": {"action": "decline"},
        },
    )
    assert result is False


def test_elicit_confirmation_requeues_unrelated_messages(monkeypatch):
    """A concurrent request arriving mid-elicitation must not be dropped."""
    import io
    import json as _json
    import sys as _sys

    import src.mcp.protocol as prot

    _initialize(prot, {"elicitation": {}})
    prot._pending_lines.clear()

    out = io.StringIO()
    monkeypatch.setattr(_sys, "stdout", out)

    unrelated = _json.dumps({"jsonrpc": "2.0", "id": 99, "method": "ping"})
    lines = [unrelated + "\n"]

    def fake_readline():
        if lines:
            return lines.pop(0)
        request = _json.loads(out.getvalue().strip().splitlines()[-1])
        return _json.dumps({
            "jsonrpc": "2.0",
            "id": request["id"],
            "result": {"action": "accept", "content": {"confirm": True}},
        }) + "\n"

    monkeypatch.setattr(_sys, "stdin", mock.Mock(readline=fake_readline))
    result = prot.elicit_confirmation("Proceed?")
    assert result is True
    assert prot._pending_lines == [unrelated]
    prot._pending_lines.clear()


def test_memory_gc_dry_run_skips_elicitation():
    """dry-run GC must never block on a confirmation round-trip."""
    import src.mcp.protocol as prot
    from src.mcp.handlers import handle_memory_gc

    _initialize(prot, {"elicitation": {}})
    with mock.patch("src.mcp.protocol.elicit_confirmation") as elicit:
        with mock.patch("src.mcp.handlers.run_gc", return_value={"blocked": False, "candidates": []}):
            handle_memory_gc({"mode": "dry-run"})
    elicit.assert_not_called()


def test_memory_gc_declined_elicitation_cancels():
    import src.mcp.protocol as prot
    from src.mcp.handlers import handle_memory_gc

    _initialize(prot, {"elicitation": {}})
    with mock.patch("src.mcp.protocol.elicit_confirmation", return_value=False):
        with mock.patch("src.mcp.handlers.run_gc") as gc:
            out = handle_memory_gc({"mode": "archive"})
    gc.assert_not_called()
    assert "Cancelled" in out


def test_signal_handlers_override_inherited_sig_ign():
    """Spawners often start children with SIGINT=SIG_IGN and Python respects it,
    so the client's shutdown SIGINT did nothing and orphan servers accumulated.
    The server must install its own handlers, overriding the inheritance."""
    import signal

    from src.mcp import protocol

    old_int = signal.getsignal(signal.SIGINT)
    old_term = signal.getsignal(signal.SIGTERM)
    try:
        signal.signal(signal.SIGINT, signal.SIG_IGN)  # simulate the bad inheritance
        protocol._install_signal_handlers()
        handler = signal.getsignal(signal.SIGINT)
        assert handler not in (signal.SIG_IGN, signal.SIG_DFL)
        assert signal.getsignal(signal.SIGTERM) is handler
        import pytest as _pytest

        with _pytest.raises(SystemExit):
            handler(signal.SIGINT, None)
    finally:
        signal.signal(signal.SIGINT, old_int)
        signal.signal(signal.SIGTERM, old_term)


def test_parent_death_watchdog_exits_when_ppid_changes(monkeypatch):
    """When the client dies abnormally (no stdin close, no signal), the server is
    reparented and its ppid changes; the watchdog must self-terminate so orphans
    can't accumulate. os._exit is the one exit that preempts a blocked main thread."""
    import threading

    from src.mcp import protocol

    # First getppid() (captured as start_ppid) returns 5000; every later poll
    # returns 6000 — the client died and we were reparented.
    calls = {"n": 0}

    def _fake_getppid():
        calls["n"] += 1
        return 5000 if calls["n"] == 1 else 6000

    exited = threading.Event()
    captured = {}

    def _fake_exit(code):
        captured["code"] = code
        exited.set()
        raise SystemExit(code)  # end the watchdog thread instead of killing pytest

    monkeypatch.setattr(protocol.os, "getppid", _fake_getppid)
    monkeypatch.setattr(protocol.os, "_exit", _fake_exit)

    protocol._start_parent_death_watchdog(poll_interval=0.01)
    assert exited.wait(timeout=2.0), "watchdog did not detect parent death"
    assert captured["code"] == 0
