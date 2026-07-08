"""Canonical registry of Engram memory item types.

Single source of truth for item_type → table mappings, FTS columns, and
ranking metadata. Replaces scattered table_map dicts across the codebase.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ItemType:
    name: str
    table: str
    fts_title_col: str
    fts_content_expr: str
    dedup_column: str | None = None
    pinnable: bool = False
    rank_multiplier: float = 1.0
    gc_eligible: bool = True


REGISTRY: dict[str, ItemType] = {
    "mistake": ItemType(
        name="mistake",
        table="mistakes",
        fts_title_col="mistake",
        fts_content_expr=(
            "context || ' | ' || mistake || ' | ' || COALESCE(root_cause,'') || ' | ' || fix"
        ),
        dedup_column="mistake",
        pinnable=True,
        rank_multiplier=1.2,
    ),
    "pattern": ItemType(
        name="pattern",
        table="patterns",
        fts_title_col="name",
        fts_content_expr="symptoms || ' | ' || root_cause || ' | ' || standard_fix",
        dedup_column="name",
        pinnable=True,
        rank_multiplier=1.1,
    ),
    "skill": ItemType(
        name="skill",
        table="skills",
        fts_title_col="name",
        fts_content_expr="trigger_desc || ' | ' || workflow || ' | ' || COALESCE(pitfalls,'')",
        dedup_column="name",
        pinnable=True,
        rank_multiplier=1.0,
    ),
    "conversation": ItemType(
        name="conversation",
        table="conversations",
        fts_title_col="title",
        fts_content_expr=(
            "COALESCE(tasks_completed,'') || ' | ' || COALESCE(key_decisions,'')"
        ),
        pinnable=False,
        rank_multiplier=0.9,
    ),
    "session": ItemType(
        name="session",
        table="sessions",
        fts_title_col="session_id",
        fts_content_expr="title || ' | ' || COALESCE(workflow_used,'')",
        pinnable=False,
        rank_multiplier=0.8,
    ),
    "role": ItemType(
        name="role",
        table="roles",
        fts_title_col="name",
        fts_content_expr="name || ' | ' || COALESCE(description,'')",
        pinnable=False,
        rank_multiplier=0.7,
        gc_eligible=False,
    ),
    "workflow": ItemType(
        name="workflow",
        table="workflows",
        fts_title_col="name",
        fts_content_expr="name || ' | ' || COALESCE(description,'')",
        pinnable=False,
        rank_multiplier=0.7,
        gc_eligible=False,
    ),
    "prompt": ItemType(
        name="prompt",
        table="prompts",
        fts_title_col="name",
        fts_content_expr="role || ' | ' || description || ' | ' || COALESCE(best_for,'')",
        pinnable=True,
        rank_multiplier=0.95,
    ),
}


def table_for(item_type: str) -> str | None:
    """Return SQL table name for item_type, or None if unknown."""
    spec = REGISTRY.get(item_type)
    return spec.table if spec else None


def fts_indexed_types() -> frozenset[str]:
    """Item types that participate in memory_fts (excludes role/workflow by default)."""
    return frozenset(
        t for t, spec in REGISTRY.items() if t in ("mistake", "pattern", "skill", "conversation", "session", "prompt")
    )


def usage_ranked_types() -> frozenset[str]:
    """Item types whose usage_count/last_used_at feed search ranking."""
    return frozenset(
        t for t, spec in REGISTRY.items() if spec.table and spec.gc_eligible
    ) | frozenset({"session"})


def gc_types() -> frozenset[str]:
    """Item types eligible for garbage collection."""
    return frozenset(t for t, spec in REGISTRY.items() if spec.gc_eligible)


def rebuild_specs() -> list[tuple[str, str, str, str]]:
    """Return (item_type, table, title_col, content_expr) for FTS rebuild."""
    return [
        (spec.name, spec.table, spec.fts_title_col, spec.fts_content_expr)
        for spec in REGISTRY.values()
        if spec.name in fts_indexed_types()
    ]


def dedup_table_map() -> dict[str, tuple[str, str]]:
    """Return item_type → (table, dedup_column) for duplicate checks."""
    return {
        t: (spec.table, spec.dedup_column)
        for t, spec in REGISTRY.items()
        if spec.dedup_column
    }


def rank_multiplier_for(item_type: str | None) -> float:
    """Per-type relevance weight for ranking; 1.0 for unknown types."""
    spec = REGISTRY.get(item_type or "")
    return spec.rank_multiplier if spec else 1.0
