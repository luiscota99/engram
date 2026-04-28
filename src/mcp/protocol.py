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


def make_response(req_id, result):
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def make_error(req_id, code, message):
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def handle_request(msg: Mapping[str, Any]) -> dict[str, Any] | None:
    method = msg.get("method", "")
    req_id = msg.get("id")
    params = msg.get("params", {})

    if method == "initialize":
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

    for line in sys.stdin:
        line = line.strip()
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

