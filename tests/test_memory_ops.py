"""Tests for src/memory_ops.py — shared memory write operations."""

from __future__ import annotations

from src.database import get_tags_for_item
from src.memory_ops import create_mistake, create_pattern, create_skill


def test_create_mistake_pattern_skill(test_db):
    conn = test_db["conn"]

    mid = create_mistake(
        conn,
        date="2026-06-01",
        context="Unit test context",
        mistake="Forgot to commit migration",
        fix="Run migrations before deploy",
        root_cause="Skipped CI",
        tags="pytest,ops",
    )
    pid = create_pattern(
        conn,
        name="Stale schema",
        symptoms="Column missing errors",
        root_cause="Migration not applied",
        standard_fix="Run engram doctor --repair",
        tags="schema",
    )
    sid = create_skill(
        conn,
        name="Apply DB migrations",
        domain="engineering",
        trigger="Schema version mismatch",
        workflow="1. Backup DB\n2. Run migrations\n3. Verify version",
        pitfalls="Never skip v6 FTS rebuild",
        tags="database",
    )
    conn.commit()

    mistake_row = conn.execute("SELECT * FROM mistakes WHERE id = ?", (mid,)).fetchone()
    assert mistake_row is not None
    assert mistake_row["mistake"] == "Forgot to commit migration"
    assert "pytest" in get_tags_for_item(conn, "mistake", mid)

    pattern_row = conn.execute("SELECT * FROM patterns WHERE id = ?", (pid,)).fetchone()
    assert pattern_row is not None
    assert pattern_row["name"] == "Stale schema"

    skill_row = conn.execute("SELECT * FROM skills WHERE id = ?", (sid,)).fetchone()
    assert skill_row is not None
    assert skill_row["trigger_desc"] == "Schema version mismatch"

    fts_rows = conn.execute(
        "SELECT item_type, item_id FROM memory_fts WHERE item_type IN ('mistake', 'pattern', 'skill')"
    ).fetchall()
    indexed = {(r["item_type"], r["item_id"]) for r in fts_rows}
    assert ("mistake", str(mid)) in indexed
    assert ("pattern", str(pid)) in indexed
    assert ("skill", str(sid)) in indexed


def test_create_conversation_chunked_short_no_parts(test_db):
    from src.memory_ops import create_conversation_chunked

    conn = test_db["conn"]
    ids = create_conversation_chunked(
        conn,
        conversation_id="conv-short",
        title="short chat",
        date="2026-07-06",
        domain="test",
        turns=[f"user: line {i}" for i in range(4)],
    )
    assert len(ids) == 1


def test_create_conversation_chunked_long_makes_overlapping_parts(test_db):
    from src.memory_ops import create_conversation_chunked

    conn = test_db["conn"]
    turns = [f"user: message number {i}" for i in range(20)]
    ids = create_conversation_chunked(
        conn,
        conversation_id="conv-long",
        title="long chat",
        date="2026-07-06",
        domain="test",
        turns=turns,
        window=8,
        stride=4,
    )
    conn.commit()
    assert len(ids) > 2  # parent + several windows

    rows = conn.execute(
        "SELECT conversation_id, title FROM conversations WHERE conversation_id LIKE 'conv-long%' ORDER BY id"
    ).fetchall()
    assert rows[0]["conversation_id"] == "conv-long"
    assert rows[1]["conversation_id"] == "conv-long#p1"
    assert "(part 1)" in rows[1]["title"]

    # every part is indexed in FTS
    n_fts = conn.execute(
        "SELECT COUNT(*) FROM memory_fts WHERE item_type='conversation'"
    ).fetchone()[0]
    assert n_fts == len(ids)
