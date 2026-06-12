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
