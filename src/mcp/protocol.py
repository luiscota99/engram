"""MCP JSON-RPC over stdio — transport and routing.

Transport: one JSON-RPC object per stdin line (NDJSON-style). Oversized payloads
are not chunked; callers should prefer references over huge inline blobs.

**Errors vs MCP ``tools/call`` results**

- ``Unknown tool``: JSON-RPC *error* object (matches JSON-RPC "-32601" method not found style).
- Handler *exception*: JSON-RPC **success** response whose ``result`` is a MCP
  ``CallToolResult``: ``{"content":[...],"isError":true}``. Tool failures surface
  as structured tool output so clients (e.g. Cursor) display them inside the tool
  turn rather than as a generic protocol error — consistent with MCP SDK
  ``CallToolResult.isError``.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any, Mapping

from src.database import init_db

from .constants import PROTOCOL_VERSION, SERVER_NAME, get_server_version
from .handlers import TOOL_HANDLERS
from .tools_schema import TOOLS

logger = logging.getLogger(__name__)

# Capabilities announced by the client at initialize; drives optional features
# like elicitation (MCP spec 2025-06-18+).
_client_capabilities: dict[str, Any] = {}

# Messages that arrived while a handler was blocked waiting for an elicitation
# response; the main loop drains these before reading stdin again.
_pending_lines: list[str] = []

_server_req_counter = 0


def make_response(req_id, result):
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def make_error(req_id, code, message):
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def client_supports_elicitation() -> bool:
    return "elicitation" in _client_capabilities


def elicit_confirmation(message: str, *, title: str = "Confirm") -> bool | None:
    """Ask the user to confirm an action via MCP elicitation (server→client request).

    Returns True/False for an explicit accept/decline, or None when the client
    does not support elicitation or the round-trip fails — callers must treat
    None as "no gate available" and preserve their pre-elicitation behavior.
    """
    global _server_req_counter
    if not client_supports_elicitation():
        return None

    _server_req_counter += 1
    req_id = f"engram-elicit-{_server_req_counter}"
    request = {
        "jsonrpc": "2.0",
        "id": req_id,
        "method": "elicitation/create",
        "params": {
            "message": message,
            "requestedSchema": {
                "type": "object",
                "properties": {
                    "confirm": {
                        "type": "boolean",
                        "title": title,
                        "description": "Set to true to proceed.",
                    }
                },
                "required": ["confirm"],
            },
        },
    }
    try:
        sys.stdout.write(json.dumps(request) + "\n")
        sys.stdout.flush()
        while True:
            raw = sys.stdin.readline()
            if not raw:
                return None  # client closed the pipe
            line = raw.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("id") == req_id and ("result" in msg or "error" in msg):
                if "error" in msg:
                    return None
                result = msg.get("result") or {}
                if result.get("action") != "accept":
                    return False
                content = result.get("content") or {}
                return bool(content.get("confirm"))
            # Not our response — requeue for the main loop.
            _pending_lines.append(line)
    except Exception:
        logger.exception("Elicitation round-trip failed")
        return None


def handle_request(msg: Mapping[str, Any]) -> dict[str, Any] | None:
    method = msg.get("method", "")
    req_id = msg.get("id")
    params = msg.get("params", {})

    if method == "initialize":
        _client_capabilities.clear()
        _client_capabilities.update(params.get("capabilities") or {})
        return make_response(
            req_id,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": get_server_version()},
            },
        )

    if method == "notifications/initialized":
        return None

    if method == "tools/list":
        return make_response(req_id, {"tools": TOOLS})

    if method == "tools/call":
        tool_name = params.get("name", "")
        tool_args = params.get("arguments", {})

        handler = TOOL_HANDLERS.get(tool_name)
        if not handler:
            return make_error(req_id, -32601, f"Unknown tool: {tool_name}")

        try:
            result_text = handler(tool_args)
            return make_response(req_id, {"content": [{"type": "text", "text": result_text}]})
        except Exception as e:
            logger.exception("Error in MCP tool %s", tool_name)
            return make_response(
                req_id,
                {
                    "content": [{"type": "text", "text": f"Error: {str(e)}"}],
                    "isError": True,
                },
            )

    if method == "ping":
        return make_response(req_id, {})

    if req_id is not None:
        return make_error(req_id, -32601, f"Method not found: {method}")

    return None


def run_stdio_server():
    """Run the MCP server over stdio (stdin/stdout)."""
    init_db()
    logger.info("Engram MCP server started (pid=%s)", os.getpid())

    # readline (not iteration) so elicit_confirmation can also read stdin
    # mid-handler; messages it sets aside are drained first.
    while True:
        if _pending_lines:
            line = _pending_lines.pop(0)
        else:
            raw = sys.stdin.readline()
            if not raw:
                break
            line = raw.strip()
        if not line:
            continue

        try:
            msg = json.loads(line)
        except json.JSONDecodeError as e:
            logger.warning("JSON parse error on stdin: %s", e)
            continue

        response = handle_request(msg)
        if response is not None:
            out = json.dumps(response)
            sys.stdout.write(out + "\n")
            sys.stdout.flush()

