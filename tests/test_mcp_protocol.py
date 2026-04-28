"""Tests for MCP stdio protocol: JSON-RPC shape and errors vs CallToolResult."""

from __future__ import annotations

from unittest import mock


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
