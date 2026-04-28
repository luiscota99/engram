#!/usr/bin/env python3
"""
Engram MCP Server — Model Context Protocol interface for persistent memory.
Implementation is split across ``src/mcp/`` (protocol transport, tool schemas, handlers).

Usage in Cursor MCP config (~/.cursor/mcp.json):
{
  "mcpServers": {
    "engram": {
      "command": "python3",
      "args": ["/path/to/engram/src/mcp_server.py"],
      "enabled": true,
      "timeout": 30
    }
  }
}
"""
from __future__ import annotations

import logging
import os
import sys

# Allow running as ``python3 src/mcp_server.py`` in addition to ``python3 -m src.mcp_server``
if __name__ == "__main__" and __package__ is None:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    __package__ = "src"

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

# Backward-compatible imports for tests and callers that imported handlers from this module
from src.mcp.handlers import handle_memory_session_review  # noqa: E402
from src.mcp.protocol import run_stdio_server  # noqa: E402

__all__ = ["run_stdio_server", "handle_memory_session_review"]


if __name__ == "__main__":
    run_stdio_server()
