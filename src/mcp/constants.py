"""MCP server identity and protocol constants."""

from __future__ import annotations

from src.version import get_package_version

SERVER_NAME = "engram"
PROTOCOL_VERSION = "2024-11-05"


def get_server_version() -> str:
    """Same version string as the ``engram-memory`` Python distribution."""
    return get_package_version()
