"""Tests for src/temporal.py — memory invalidation / supersession."""

from __future__ import annotations

import os

import pytest

from src.database import get_connection, init_db
from src.memory_ops import create_mistake
from src.temporal import invalidate_memory


@pytest.fixture
def temporal_db(tmp_path):
    db_path = str(tmp_path / "temporal.db")
    os.environ["ENGRAM_DB_PATH"] = db_path
    init_db(db_path)
    return db_path


def test_invalidate_memory_marks_title_with_superseded(temporal_db):
    with get_connection(temporal_db) as conn:
        mid = create_mistake(
            conn,
            date="2026-06-01",
            context="pytest",
            mistake="Original mistake title",
            fix="Apply the fix",
        )
        conn.commit()

    assert invalidate_memory("mistake", mid, db_path=temporal_db) is True

    with get_connection(temporal_db) as conn:
        row = conn.execute("SELECT mistake FROM mistakes WHERE id = ?", (mid,)).fetchone()
        assert row is not None
        assert str(row["mistake"]).startswith("[SUPERSEDED]")
        assert "Original mistake title" in row["mistake"]
