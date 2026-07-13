"""Coverage tests for src/mcp/protocol.py — JSON-RPC transport and routing."""

from __future__ import annotations

import io
import json
import os

import pytest

from src.database import init_db
from src.mcp import protocol


@pytest.fixture
def mcp_db(tmp_path):
    """Temporary DB with ENGRAM_DB_PATH set (function-scoped)."""
    db_path = str(tmp_path / "protocol.db")
    os.environ["ENGRAM_DB_PATH"] = db_path
    init_db(db_path)
    return db_path


@pytest.fixture(autouse=True)
def _reset_protocol_globals():
    """Keep the module-level elicitation state hermetic across tests."""
    protocol._client_capabilities.clear()
    protocol._pending_lines.clear()
    protocol._server_req_counter = 0
    yield
    protocol._client_capabilities.clear()
    protocol._pending_lines.clear()
    protocol._server_req_counter = 0


# --------------------------------------------------------------------------- #
# make_response / make_error
# --------------------------------------------------------------------------- #


def test_make_response_shape():
    resp = protocol.make_response(7, {"ok": 1})
    assert resp == {"jsonrpc": "2.0", "id": 7, "result": {"ok": 1}}


def test_make_error_shape():
    err = protocol.make_error(3, -32601, "boom")
    assert err == {
        "jsonrpc": "2.0",
        "id": 3,
        "error": {"code": -32601, "message": "boom"},
    }


# --------------------------------------------------------------------------- #
# handle_request routing
# --------------------------------------------------------------------------- #


def test_initialize_records_capabilities_and_returns_server_info():
    from src.mcp.constants import PROTOCOL_VERSION, SERVER_NAME, get_server_version

    resp = protocol.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"capabilities": {"elicitation": {}}},
        }
    )
    result = resp["result"]
    assert result["protocolVersion"] == PROTOCOL_VERSION
    assert result["serverInfo"]["name"] == SERVER_NAME
    assert result["serverInfo"]["version"] == get_server_version()
    assert result["capabilities"] == {"tools": {}}
    # Side effect: capabilities captured for later elicitation checks.
    assert protocol.client_supports_elicitation() is True


def test_initialize_clears_previous_capabilities():
    protocol._client_capabilities["elicitation"] = {}
    protocol.handle_request(
        {"id": 1, "method": "initialize", "params": {"capabilities": {}}}
    )
    assert protocol.client_supports_elicitation() is False


def test_notifications_initialized_returns_none():
    assert (
        protocol.handle_request({"method": "notifications/initialized"}) is None
    )


def test_tools_list_returns_static_tools(mcp_db):
    resp = protocol.handle_request({"id": 2, "method": "tools/list"})
    tools = resp["result"]["tools"]
    names = {t["name"] for t in tools}
    assert "memory_search" in names
    assert len(tools) >= 1


def test_tools_list_survives_reflex_import_failure(monkeypatch):
    import src.reflex as reflex

    def _boom():
        raise RuntimeError("reflex listing broken")

    monkeypatch.setattr(reflex, "reflex_tools_for_mcp", _boom)
    resp = protocol.handle_request({"id": 2, "method": "tools/list"})
    # Static tools still returned despite the reflex extension failing.
    names = {t["name"] for t in resp["result"]["tools"]}
    assert "memory_search" in names


def test_tools_call_success_returns_text_content(mcp_db):
    resp = protocol.handle_request(
        {
            "id": 5,
            "method": "tools/call",
            "params": {"name": "memory_health", "arguments": {}},
        }
    )
    result = resp["result"]
    assert result.get("isError") is None
    assert result["content"][0]["type"] == "text"
    assert "Memory Health Report" in result["content"][0]["text"]


def test_tools_call_handler_exception_is_structured_tool_error(monkeypatch):
    def _boom(_args):
        raise ValueError("handler exploded")

    monkeypatch.setitem(protocol.TOOL_HANDLERS, "memory_health", _boom)
    resp = protocol.handle_request(
        {
            "id": 6,
            "method": "tools/call",
            "params": {"name": "memory_health", "arguments": {}},
        }
    )
    result = resp["result"]
    assert result["isError"] is True
    assert result["content"][0]["text"] == "Error: handler exploded"


def test_tools_call_unknown_tool_is_jsonrpc_error():
    resp = protocol.handle_request(
        {
            "id": 8,
            "method": "tools/call",
            "params": {"name": "no_such_tool", "arguments": {}},
        }
    )
    assert resp["error"]["code"] == -32601
    assert "Unknown tool: no_such_tool" in resp["error"]["message"]


def test_tools_call_reflex_dispatch(monkeypatch):
    import src.reflex as reflex

    captured = {}

    def _fake_handle(name, args):
        captured["name"] = name
        captured["args"] = args
        return "reflex-result-text"

    monkeypatch.setattr(reflex, "handle_reflex_call", _fake_handle)
    resp = protocol.handle_request(
        {
            "id": 9,
            "method": "tools/call",
            "params": {"name": "reflex_deploy", "arguments": {"x": 1}},
        }
    )
    assert resp["result"]["content"][0]["text"] == "reflex-result-text"
    assert captured == {"name": "reflex_deploy", "args": {"x": 1}}


def test_ping_returns_empty_result():
    resp = protocol.handle_request({"id": 11, "method": "ping"})
    assert resp == {"jsonrpc": "2.0", "id": 11, "result": {}}


def test_unknown_method_with_id_returns_method_not_found():
    resp = protocol.handle_request({"id": 12, "method": "frobnicate"})
    assert resp["error"]["code"] == -32601
    assert "Method not found: frobnicate" in resp["error"]["message"]


def test_unknown_notification_without_id_returns_none():
    # No id => a notification; unknown method must not produce a response.
    assert protocol.handle_request({"method": "frobnicate"}) is None


# --------------------------------------------------------------------------- #
# elicitation
# --------------------------------------------------------------------------- #


def test_client_supports_elicitation_toggle():
    assert protocol.client_supports_elicitation() is False
    protocol._client_capabilities["elicitation"] = {}
    assert protocol.client_supports_elicitation() is True


def test_elicit_confirmation_no_support_returns_none():
    assert protocol.elicit_confirmation("proceed?") is None


def _wire_streams(monkeypatch, stdin_text):
    """Point protocol's sys.stdin/stdout at in-memory streams; return stdout."""
    protocol._client_capabilities["elicitation"] = {}
    out = io.StringIO()
    monkeypatch.setattr(protocol.sys, "stdin", io.StringIO(stdin_text))
    monkeypatch.setattr(protocol.sys, "stdout", out)
    return out


def test_elicit_confirmation_accept_true(monkeypatch):
    response = json.dumps(
        {"id": "engram-elicit-1", "result": {"action": "accept", "content": {"confirm": True}}}
    )
    out = _wire_streams(monkeypatch, response + "\n")
    assert protocol.elicit_confirmation("proceed?", title="Go") is True
    # The server emitted a well-formed elicitation/create request.
    sent = json.loads(out.getvalue().strip())
    assert sent["method"] == "elicitation/create"
    assert sent["id"] == "engram-elicit-1"
    assert sent["params"]["message"] == "proceed?"
    assert sent["params"]["requestedSchema"]["properties"]["confirm"]["title"] == "Go"


def test_elicit_confirmation_confirm_false(monkeypatch):
    response = json.dumps(
        {"id": "engram-elicit-1", "result": {"action": "accept", "content": {"confirm": False}}}
    )
    _wire_streams(monkeypatch, response + "\n")
    assert protocol.elicit_confirmation("proceed?") is False


def test_elicit_confirmation_declined_action_returns_false(monkeypatch):
    response = json.dumps({"id": "engram-elicit-1", "result": {"action": "decline"}})
    _wire_streams(monkeypatch, response + "\n")
    assert protocol.elicit_confirmation("proceed?") is False


def test_elicit_confirmation_error_response_returns_none(monkeypatch):
    response = json.dumps({"id": "engram-elicit-1", "error": {"code": 1, "message": "no"}})
    _wire_streams(monkeypatch, response + "\n")
    assert protocol.elicit_confirmation("proceed?") is None


def test_elicit_confirmation_pipe_closed_returns_none(monkeypatch):
    _wire_streams(monkeypatch, "")  # EOF immediately
    assert protocol.elicit_confirmation("proceed?") is None


def test_elicit_confirmation_skips_blank_and_bad_json_then_matches(monkeypatch):
    lines = [
        "",  # blank -> skipped
        "{not json",  # decode error -> skipped
        json.dumps(
            {"id": "engram-elicit-1", "result": {"action": "accept", "content": {"confirm": True}}}
        ),
    ]
    _wire_streams(monkeypatch, "\n".join(lines) + "\n")
    assert protocol.elicit_confirmation("proceed?") is True


def test_elicit_confirmation_requeues_unrelated_message(monkeypatch):
    other = json.dumps({"id": "other-req", "method": "tools/list"})
    match = json.dumps(
        {"id": "engram-elicit-1", "result": {"action": "accept", "content": {"confirm": True}}}
    )
    _wire_streams(monkeypatch, other + "\n" + match + "\n")
    assert protocol.elicit_confirmation("proceed?") is True
    # The unrelated message was set aside for the main loop.
    assert other in protocol._pending_lines


def test_elicit_confirmation_write_failure_returns_none(monkeypatch):
    protocol._client_capabilities["elicitation"] = {}

    class _BrokenStdout:
        def write(self, _s):
            raise OSError("pipe broke")

        def flush(self):
            pass

    monkeypatch.setattr(protocol.sys, "stdout", _BrokenStdout())
    monkeypatch.setattr(protocol.sys, "stdin", io.StringIO(""))
    assert protocol.elicit_confirmation("proceed?") is None


# --------------------------------------------------------------------------- #
# run_stdio_server
# --------------------------------------------------------------------------- #


def test_run_stdio_server_handles_requests_and_bad_json(mcp_db, monkeypatch):
    lines = [
        "{bad json",  # parse error -> logged, no output
        json.dumps({"id": 1, "method": "ping"}),
        json.dumps({"method": "notifications/initialized"}),  # no response written
        "",  # blank line -> skipped
        json.dumps({"id": 2, "method": "ping"}),
    ]
    out = io.StringIO()
    monkeypatch.setattr(protocol.sys, "stdin", io.StringIO("\n".join(lines) + "\n"))
    monkeypatch.setattr(protocol.sys, "stdout", out)

    protocol.run_stdio_server()

    responses = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
    # Exactly two ping responses; bad-json/notification/blank produced nothing.
    assert responses == [
        {"jsonrpc": "2.0", "id": 1, "result": {}},
        {"jsonrpc": "2.0", "id": 2, "result": {}},
    ]


def test_run_stdio_server_drains_pending_lines_first(mcp_db, monkeypatch):
    # A line staged in _pending_lines is consumed before stdin is read.
    protocol._pending_lines.append(json.dumps({"id": 99, "method": "ping"}))
    out = io.StringIO()
    monkeypatch.setattr(protocol.sys, "stdin", io.StringIO(""))  # EOF after drain
    monkeypatch.setattr(protocol.sys, "stdout", out)

    protocol.run_stdio_server()

    responses = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
    assert responses == [{"jsonrpc": "2.0", "id": 99, "result": {}}]
    assert protocol._pending_lines == []
