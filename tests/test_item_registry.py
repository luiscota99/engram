"""Tests for src/item_registry.py — canonical item type registry."""

from __future__ import annotations

from src.item_registry import (
    REGISTRY,
    fts_indexed_types,
    rebuild_specs,
    table_for,
    usage_ranked_types,
)


def test_registry_keys_match_fts_indexed_rebuild_specs():
    """Every rebuild_specs entry must be an fts_indexed REGISTRY key, and vice versa."""
    indexed = fts_indexed_types()
    spec_types = {item_type for item_type, *_ in rebuild_specs()}

    assert spec_types == indexed
    assert spec_types <= set(REGISTRY.keys())
    for item_type in indexed:
        assert item_type in REGISTRY


def test_registry_session_exists():
    assert "session" in REGISTRY
    assert REGISTRY["session"].name == "session"
    assert REGISTRY["session"].table == "sessions"


def test_table_for_mistake():
    assert table_for("mistake") == "mistakes"


def test_usage_ranked_types_includes_session():
    assert "session" in usage_ranked_types()
