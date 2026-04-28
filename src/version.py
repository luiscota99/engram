"""Package version resolution (single source aligned with pyproject)."""

from __future__ import annotations


def get_package_version() -> str:
    """Return installed distribution version for ``engram-memory``, or a fallback."""
    try:
        from importlib.metadata import version

        return version("engram-memory")
    except Exception:
        return "0.1.0"
