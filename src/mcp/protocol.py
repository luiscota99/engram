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


class ElicitationFailed(RuntimeError):
    """A confirmation was requested but the round-trip broke.

    Distinct from "client doesn't support elicitation" (None): here the user
    was (or may have been) shown a prompt and never answered. Gates on
    mutating/destructive actions must fail CLOSED on this — proceeding would
    run an action after an unanswered confirmation.
    """


def elicit_confirmation(message: str, *, title: str = "Confirm") -> bool | None:
    """Ask the user to confirm an action via MCP elicitation (server→client request).

    Returns True/False for an explicit accept/decline, or None when the client
    does not support elicitation — callers treat None as "no gate available"
    and preserve their pre-elicitation behavior. Raises ``ElicitationFailed``
    when the client *does* support elicitation but the round-trip fails
    (pipe closed, error response, write failure) — callers must not proceed.
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
                raise ElicitationFailed("client closed the pipe mid-confirmation")
            line = raw.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("id") == req_id and ("result" in msg or "error" in msg):
                if "error" in msg:
                    raise ElicitationFailed(f"client returned an error: {msg['error']}")
                result = msg.get("result") or {}
                if result.get("action") != "accept":
                    return False
                content = result.get("content") or {}
                return bool(content.get("confirm"))
            # Not our response — requeue for the main loop.
            _pending_lines.append(line)
    except ElicitationFailed:
        raise
    except Exception as e:
        logger.exception("Elicitation round-trip failed")
        raise ElicitationFailed(str(e)) from e


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
        # Static tools plus any approved reflexes, exposed as first-class
        # `reflex_<name>` tools so agents invoke proven workflows deterministically.
        tools = list(TOOLS)
        try:
            from src.reflex import reflex_tools_for_mcp

            tools.extend(reflex_tools_for_mcp())
        except Exception:
            logger.debug("reflex tool listing skipped", exc_info=True)
        return make_response(req_id, {"tools": tools})

    if method == "tools/call":
        tool_name = params.get("name", "")
        tool_args = params.get("arguments", {})

        handler = TOOL_HANDLERS.get(tool_name)
        if handler is None and tool_name.startswith("reflex_"):
            from src.reflex import handle_reflex_call

            handler = lambda a, _n=tool_name: handle_reflex_call(_n, a)  # noqa: E731

        if handler is None:
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


def _install_signal_handlers() -> None:
    """Make SIGINT/SIGTERM actually terminate the server.

    Spawners often start children with SIGINT set to SIG_IGN (POSIX shells do it
    for backgrounded jobs), and Python *respects* an inherited SIG_IGN — it never
    installs its KeyboardInterrupt handler. The observed result: the MCP client's
    shutdown SIGINT does nothing ("SIGINT failed, sending SIGTERM" in client
    logs), cleanup times out, and orphaned servers accumulate holding DB
    connections — which then starves fresh servers into intermittent timeouts.
    Installing explicit handlers overrides the inherited disposition.
    """
    import signal

    def _terminate(signum, frame):  # noqa: ARG001
        logger.info("Engram MCP server exiting on signal %s (pid=%s)", signum, os.getpid())
        raise SystemExit(0)

    try:
        signal.signal(signal.SIGINT, _terminate)
        signal.signal(signal.SIGTERM, _terminate)
    except (ValueError, OSError):  # non-main thread / unsupported platform
        logger.debug("could not install signal handlers", exc_info=True)


def _start_parent_death_watchdog(poll_interval: float = 2.0) -> None:
    """Self-terminate if the parent (the MCP client) dies without closing stdin.

    stdin EOF is the portable graceful-shutdown signal and the read loop honors
    it; SIGINT/SIGTERM cover the client's ordered teardown. Neither fires when
    the client dies *abnormally* — SIGKILL, a closed terminal, system sleep: no
    stdin close, no signal. The server then blocks in ``readline`` forever and
    is reparented to init/launchd, accumulating as an orphan that holds a DB
    connection (the root cause behind the "intermittent MCP" reports).

    A daemon thread polls ``os.getppid()``; on POSIX an orphaned child reparents
    so its ppid becomes 1 (or another subreaper). When that happens the thread
    calls ``os._exit`` — the one exit that preempts the main thread even while
    it is stuck in a non-interruptible C call (SQLite/Ollama), which a signal or
    ``SystemExit`` cannot. Portable across macOS and Linux (no ``PR_SET_PDEATHSIG``,
    which is Linux-only).
    """
    if not hasattr(os, "getppid"):  # non-POSIX; nothing to watch
        return

    start_ppid = os.getppid()

    def _watch() -> None:
        import time

        while True:
            time.sleep(poll_interval)
            ppid = os.getppid()
            if ppid != start_ppid or ppid == 1:
                logger.info(
                    "Engram MCP parent gone (ppid %s→%s); exiting orphan (pid=%s)",
                    start_ppid,
                    ppid,
                    os.getpid(),
                )
                sys.stderr.flush()
                os._exit(0)

    import threading

    threading.Thread(target=_watch, name="engram-parent-watchdog", daemon=True).start()


def run_stdio_server():
    """Run the MCP server over stdio (stdin/stdout)."""
    _install_signal_handlers()
    _start_parent_death_watchdog()
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

