"""MCP package — protocol transport and tool implementations."""

from __future__ import annotations

from .protocol import handle_request, run_stdio_server

__all__ = ["handle_request", "run_stdio_server"]
