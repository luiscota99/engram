"""
Database module — schema, connection, and migrations for Engram.
Uses SQLite with FTS5 for full-text search. Zero external dependencies.
"""

import os
import sqlite3
from contextlib import contextmanager

DEFAULT_DB_PATH = os.path.join(
    os.path.expanduser("~"), ".engram", "memory.db"
)

DB_PATH = os.environ.get("ENGRAM_DB_PATH", DEFAULT_DB_PATH)

SCHEMA_VERSION = 1

SCHEMA_SQL = """
-- Mistakes: individual error instances with root cause analysis
CREATE TABLE IF NOT EXISTS mistakes (
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

-- Patterns: recurring issue types with standard solutions
CREATE TABLE IF NOT EXISTS patterns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    symptoms TEXT NOT NULL,
    root_cause TEXT NOT NULL,
    standard_fix TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now'))
);

-- Track where each pattern has been observed
CREATE TABLE IF NOT EXISTS pattern_occurrences (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern_id INTEGER NOT NULL REFERENCES patterns(id) ON DELETE CASCADE,
    conversation_id TEXT,
    date TEXT,
    notes TEXT
);

-- Skills: reusable workflows extracted from repeated tasks
CREATE TABLE IF NOT EXISTS skills (
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

-- Conversations: structured index of past sessions
CREATE TABLE IF NOT EXISTS conversations (
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

-- Tags
CREATE TABLE IF NOT EXISTS tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS item_tags (
    item_type TEXT NOT NULL,
    item_id INTEGER NOT NULL,
    tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY (item_type, item_id, tag_id)
);

-- Full-text search index across all memory types
CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
    item_type,
    item_id UNINDEXED,
    title,
    content,
    tags
);

-- Schema version tracking
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def get_db_path():
    """Return the resolved database path."""
    return DB_PATH


@contextmanager
def get_connection(db_path=None):
    """Context manager for database connections with WAL mode and foreign keys."""
    path = db_path or DB_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path=None):
    """Create tables and FTS index if they don't exist."""
    with get_connection(db_path) as conn:
        conn.executescript(SCHEMA_SQL)
        # Set schema version if not present
        existing = conn.execute(
            "SELECT value FROM schema_meta WHERE key='version'"
        ).fetchone()
        if not existing:
            conn.execute(
                "INSERT INTO schema_meta (key, value) VALUES ('version', ?)",
                (str(SCHEMA_VERSION),),
            )
    return True


def ensure_tag(conn, tag_name):
    """Get or create a tag, return its id."""
    tag_name = tag_name.strip().lower()
    row = conn.execute("SELECT id FROM tags WHERE name = ?", (tag_name,)).fetchone()
    if row:
        return row["id"]
    cursor = conn.execute("INSERT INTO tags (name) VALUES (?)", (tag_name,))
    return cursor.lastrowid


def link_tags(conn, item_type, item_id, tag_names):
    """Associate a list of tag names with an item."""
    for tag_name in tag_names:
        if not tag_name.strip():
            continue
        tag_id = ensure_tag(conn, tag_name)
        conn.execute(
            "INSERT OR IGNORE INTO item_tags (item_type, item_id, tag_id) VALUES (?, ?, ?)",
            (item_type, item_id, tag_id),
        )


def get_tags_for_item(conn, item_type, item_id):
    """Return list of tag names for an item."""
    rows = conn.execute(
        """SELECT t.name FROM tags t
           JOIN item_tags it ON t.id = it.tag_id
           WHERE it.item_type = ? AND it.item_id = ?
           ORDER BY t.name""",
        (item_type, item_id),
    ).fetchall()
    return [r["name"] for r in rows]


def index_in_fts(conn, item_type, item_id, title, content, tags_list):
    """Insert or replace an item in the FTS index."""
    # Remove old entry if exists
    conn.execute(
        "DELETE FROM memory_fts WHERE item_type = ? AND item_id = ?",
        (item_type, str(item_id)),
    )
    tags_str = " ".join(tags_list) if tags_list else ""
    conn.execute(
        "INSERT INTO memory_fts (item_type, item_id, title, content, tags) VALUES (?, ?, ?, ?, ?)",
        (item_type, str(item_id), title, content, tags_str),
    )
