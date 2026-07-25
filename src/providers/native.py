"""Native SQLite MemoryProvider wrapping Engram's default store."""

from __future__ import annotations

from typing import Any

from src.database import get_item as db_get_item
from src.search import search as memory_search
from src.temporal import invalidate_memory


class NativeSqliteProvider:
    """Default provider — one ``memory.db`` via existing modules."""

    def __init__(self, db_path: str | None = None):
        self.db_path = db_path

    def search(self, query: str, **kwargs: Any) -> list[dict]:
        kwargs.setdefault("db_path", self.db_path)
        return memory_search(query, **kwargs)

    def get_item(self, item_type: str, item_id: int) -> dict | None:
        return db_get_item(item_type, item_id, db_path=self.db_path)

    def invalidate(
        self,
        item_type: str,
        item_id: int,
        *,
        superseded_by: int | None = None,
        reason: str | None = None,
    ) -> bool:
        return invalidate_memory(
            item_type,
            item_id,
            superseded_by=superseded_by,
            reason=reason,
            db_path=self.db_path,
        )


_DEFAULT: NativeSqliteProvider | None = None


def get_provider(db_path: str | None = None) -> NativeSqliteProvider:
    """Process-wide default provider (overridable in tests)."""
    global _DEFAULT
    if db_path is not None:
        return NativeSqliteProvider(db_path=db_path)
    if _DEFAULT is None:
        _DEFAULT = NativeSqliteProvider()
    return _DEFAULT


def set_provider(provider: NativeSqliteProvider | None) -> None:
    global _DEFAULT
    _DEFAULT = provider
