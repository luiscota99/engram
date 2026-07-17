"""Tests for src/migrations.py — sequential schema upgrades."""

from __future__ import annotations

import os

import pytest

try:
    import sqlean as sqlite3
except ImportError:
    import sqlite3

try:
    import sqlite_vec
except ImportError:
    sqlite_vec = None

from src.database import SCHEMA_VERSION
from src.migrations import run_migrations

# Minimal v1 schema — core tables before migrations v2+.
V1_SCHEMA_SQL = """
CREATE TABLE mistakes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    context TEXT NOT NULL,
    mistake TEXT NOT NULL,
    root_cause TEXT,
    fix TEXT NOT NULL,
    prevention TEXT,
    conversation_id TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE patterns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    symptoms TEXT NOT NULL,
    root_cause TEXT NOT NULL,
    standard_fix TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE pattern_occurrences (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern_id INTEGER NOT NULL REFERENCES patterns(id) ON DELETE CASCADE,
    conversation_id TEXT,
    date TEXT,
    notes TEXT
);
CREATE TABLE skills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    domain TEXT NOT NULL,
    trigger_desc TEXT NOT NULL,
    workflow TEXT NOT NULL,
    pitfalls TEXT,
    key_files TEXT,
    dependencies TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    date TEXT NOT NULL,
    domain TEXT NOT NULL,
    tasks_completed TEXT,
    key_decisions TEXT,
    mistakes_summary TEXT,
    skills_extracted TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE prompts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    role TEXT NOT NULL,
    domain TEXT NOT NULL,
    description TEXT NOT NULL,
    prompt_text TEXT NOT NULL,
    source_path TEXT,
    best_for TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE
);
CREATE TABLE item_tags (
    item_type TEXT NOT NULL,
    item_id INTEGER NOT NULL,
    tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY (item_type, item_id, tag_id)
);
CREATE VIRTUAL TABLE memory_fts USING fts5(
    item_type,
    item_id UNINDEXED,
    title,
    content,
    tags
);
CREATE TABLE schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def _create_v1_database(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    if sqlite_vec is not None:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS vec_memory USING vec0(embedding float[768])"
        )
    conn.executescript(V1_SCHEMA_SQL)
    conn.execute("INSERT INTO schema_meta (key, value) VALUES ('version', '1')")
    conn.commit()
    conn.close()


@pytest.fixture
def migration_db(tmp_path):
    db_path = str(tmp_path / "migration_v1.db")
    os.environ["ENGRAM_DB_PATH"] = db_path
    _create_v1_database(db_path)
    return db_path


def _open_conn(db_path: str):
    """Open DB without auto-migration (get_connection migrates on connect)."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    if sqlite_vec is not None:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
    return conn


def test_migrations_v1_to_schema_version(migration_db):
    conn = _open_conn(migration_db)
    try:
        current = int(
            conn.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone()["value"]
        )
        assert current == 1

        run_migrations(conn, 1, SCHEMA_VERSION)
        conn.commit()

        final = int(
            conn.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone()["value"]
        )
        assert final == SCHEMA_VERSION

        # Spot-check objects introduced by later migrations
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "_test_migration_v2" in tables  # v2
        assert "sessions" in tables  # v5
        assert "embedding_status" in tables  # v8
        assert "item_pins" in tables  # v10
        assert "memory_facts" in tables  # v11

        mistake_cols = {
            row["name"] for row in conn.execute("PRAGMA table_info(mistakes)").fetchall()
        }
        assert "usage_count" in mistake_cols  # v3
        assert "superseded_by" in mistake_cols  # v11
    finally:
        conn.close()


def _tables_and_columns(conn) -> dict:
    """Map of table name -> set of column names (user tables only)."""
    tables = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        if not row["name"].startswith("sqlite_")
        and not row["name"].startswith("memory_fts_")   # FTS5 shadow tables
        and not row["name"].startswith("vec_memory_")   # vec0 shadow tables
        and row["name"] != "_test_migration_v2"          # migration self-test artifact
    }
    return {
        t: {row["name"] for row in conn.execute(f"PRAGMA table_info({t})").fetchall()}
        for t in tables
    }


def test_fresh_schema_matches_migrated_schema(migration_db, tmp_path):
    """Regression: v11 added memory_facts + superseded_by via migration only,
    so fresh databases crashed on memory_invalidate. Baseline SCHEMA_SQL must
    stay in lockstep with the migration chain."""
    from src.database import init_db

    conn = _open_conn(migration_db)
    try:
        run_migrations(conn, 1, SCHEMA_VERSION)
        conn.commit()
        migrated = _tables_and_columns(conn)
    finally:
        conn.close()

    fresh_path = str(tmp_path / "fresh.db")
    init_db(fresh_path)
    fresh_conn = _open_conn(fresh_path)
    try:
        fresh = _tables_and_columns(fresh_conn)
    finally:
        fresh_conn.close()

    missing_tables = set(migrated) - set(fresh)
    assert not missing_tables, f"Fresh DB is missing tables that migrations create: {missing_tables}"

    for table, cols in migrated.items():
        missing_cols = cols - fresh[table]
        assert not missing_cols, f"Fresh DB table {table!r} is missing columns: {missing_cols}"


def test_v12_normalizes_stored_vectors(tmp_path):
    """Migration v12 rescales legacy unnormalized vectors to unit length."""
    import json
    import math

    from src.database import get_connection, init_db

    db = str(tmp_path / "norm.db")
    init_db(db)
    with get_connection(db) as conn:
        conn.execute(
            "INSERT INTO vec_memory(rowid, embedding) VALUES (1, ?)",
            (json.dumps([3.0] * 768),),  # norm far from 1
        )
        conn.execute(
            "UPDATE schema_meta SET value = '11' WHERE key = 'version'"
        )

    with get_connection(db) as conn:  # reopening triggers migration to v12
        raw = conn.execute("SELECT embedding FROM vec_memory WHERE rowid = 1").fetchone()[0]
        import struct
        vec = list(struct.unpack(f"{len(raw)//4}f", raw)) if isinstance(raw, (bytes, bytearray)) else json.loads(raw)
        norm = math.sqrt(sum(x * x for x in vec))
        assert abs(norm - 1.0) < 1e-3


# ── v23: single-owner FTS writes (triggers dropped, drift repaired) ──

def test_v23_drops_all_fts_triggers(migration_db):
    """v6 created FTS triggers; v23 must remove every one of them."""
    conn = _open_conn(migration_db)
    try:
        run_migrations(conn, 1, SCHEMA_VERSION)
        conn.commit()
        triggers = [
            r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'fts_%'"
            ).fetchall()
        ]
        assert triggers == []
    finally:
        conn.close()


def test_v23_repairs_duplicate_and_orphan_integer_rows(migration_db):
    """Seed the exact corruption the dual write path produced, then migrate."""
    conn = _open_conn(migration_db)
    try:
        run_migrations(conn, 1, 22)
        conn.commit()
        # dup pair: good text row + degraded integer twin (observed live 2026-07-17)
        conn.execute(
            "INSERT INTO memory_fts (item_type, item_id, title, content, tags) "
            "VALUES ('mistake', '7', 'good row', 'full content', 'tag1 tag2')"
        )
        conn.execute(
            "INSERT INTO memory_fts (item_type, item_id, title, content, tags) "
            "VALUES ('mistake', 7, 'good row', 'degraded', '')"
        )
        # orphan: only the integer row survives (update trigger ate the text row)
        conn.execute(
            "INSERT INTO memory_fts (item_type, item_id, title, content, tags) "
            "VALUES ('skill', 3, 'orphan skill', 'trigger content', '')"
        )
        conn.commit()

        run_migrations(conn, 22, 23)
        conn.commit()

        rows = conn.execute(
            "SELECT item_type, item_id, typeof(item_id) AS ty, title, tags "
            "FROM memory_fts ORDER BY item_type, item_id"
        ).fetchall()
        # every row is text-typed, no duplicates
        assert all(r["ty"] == "text" for r in rows)
        keys = [(r["item_type"], r["item_id"]) for r in rows]
        assert len(keys) == len(set(keys))
        # the good text row survived with its tags; the dup twin is gone
        mistake = [r for r in rows if r["item_type"] == "mistake"]
        assert len(mistake) == 1 and mistake[0]["tags"] == "tag1 tag2"
        # the orphan was re-keyed to text, content preserved
        skill = [r for r in rows if r["item_type"] == "skill"]
        assert len(skill) == 1 and skill[0]["item_id"] == "3"
        assert skill[0]["title"] == "orphan skill"
    finally:
        conn.close()


def test_add_then_update_keeps_single_text_fts_row(tmp_path, monkeypatch):
    """Regression for the corruption generator: a core-table UPDATE (usage
    bump) must not duplicate or degrade the item's FTS row."""
    db = str(tmp_path / "single_owner.db")
    monkeypatch.setenv("ENGRAM_DB_PATH", db)
    from src.database import get_connection, init_db, record_usage
    from src.memory_ops import create_mistake

    init_db(db)
    with get_connection(db) as conn:
        create_mistake(
            conn, date="2026-07-17", context="ctx",
            mistake="single owner check", fix="a fix", tags="alpha beta",
        )
    record_usage("mistake", 1, db_path=db)  # UPDATE on the core table

    with get_connection(db) as conn:
        rows = conn.execute(
            "SELECT typeof(item_id) AS ty, tags FROM memory_fts "
            "WHERE item_type='mistake' AND CAST(item_id AS TEXT)='1'"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["ty"] == "text"
    assert "alpha" in rows[0]["tags"]


def test_integrity_report_flags_second_writer(tmp_path, monkeypatch):
    """doctor's invariants must catch dup groups and non-text ids directly."""
    db = str(tmp_path / "invariants.db")
    monkeypatch.setenv("ENGRAM_DB_PATH", db)
    from src.database import get_connection, init_db
    from src.doctor import integrity_report

    init_db(db)
    clean = integrity_report(db_path=db)
    assert clean["fts_dup_groups"] == 0 and clean["fts_type_drift"] == 0

    with get_connection(db) as conn:
        conn.execute(
            "INSERT INTO memory_fts (item_type, item_id, title, content, tags) "
            "VALUES ('mistake', '9', 'a', 'b', '')"
        )
        conn.execute(
            "INSERT INTO memory_fts (item_type, item_id, title, content, tags) "
            "VALUES ('mistake', 9, 'a', 'b', '')"
        )
    dirty = integrity_report(db_path=db)
    assert dirty["fts_dup_groups"] == 1
    assert dirty["fts_type_drift"] == 1
