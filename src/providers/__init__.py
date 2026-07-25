"""Memory provider protocol — swap storage without rewriting MCP/CLI."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class MemoryProvider(Protocol):
    """Minimal surface used by search/capture/invalidate paths."""

    def search(self, query: str, **kwargs: Any) -> list[dict]:
        ...

    def get_item(self, item_type: str, item_id: int) -> dict | None:
        ...

    def invalidate(
        self,
        item_type: str,
        item_id: int,
        *,
        superseded_by: int | None = None,
        reason: str | None = None,
    ) -> bool:
        ...


# MemPalace docs alias — same protocol, different name in competitive literature.
StorageBackend = MemoryProvider

from .native import NativeSqliteProvider, get_provider, set_provider  # noqa: E402

__all__ = [
    "MemoryProvider",
    "StorageBackend",
    "NativeSqliteProvider",
    "get_provider",
    "set_provider",
]
