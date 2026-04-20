"""
Database module — schema, connection, and migrations for Engram.
Uses SQLite with FTS5 for full-text search. Zero external dependencies.
"""

import json
import os
from contextlib import contextmanager

try:
    import sqlean as sqlite3
except ImportError:
    import sqlite3

try:
    import sqlite_vec
except ImportError:
    sqlite_vec = None

from .embeddings import embed_text
from .migrations import run_migrations

DEFAULT_DB_PATH = os.path.join(os.path.expanduser("~"), ".engram", "memory.db")

DB_PATH = os.environ.get("ENGRAM_DB_PATH", DEFAULT_DB_PATH)

SCHEMA_VERSION = 4

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
    usage_count INTEGER DEFAULT 0,
    last_used_at TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

-- Patterns: recurring issue types with standard solutions
CREATE TABLE IF NOT EXISTS patterns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    symptoms TEXT NOT NULL,
    root_cause TEXT NOT NULL,
    standard_fix TEXT NOT NULL,
    usage_count INTEGER DEFAULT 0,
    last_used_at TEXT,
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
    usage_count INTEGER DEFAULT 0,
    last_used_at TEXT,
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
    usage_count INTEGER DEFAULT 0,
    last_used_at TEXT,
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

-- Prompts: reusable LLM system prompts for specialized tasks
CREATE TABLE IF NOT EXISTS prompts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    role TEXT NOT NULL,
    domain TEXT NOT NULL,
    description TEXT NOT NULL,
    prompt_text TEXT NOT NULL,
    source_path TEXT,
    best_for TEXT,
    usage_count INTEGER DEFAULT 0,
    last_used_at TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

-- Schema version tracking
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

-- Vector search table (sqlite-vec)
-- Contains embeddings for all memory items to enable semantic search
-- dimension 768 matches nomic-embed-text / typical embedding models
CREATE VIRTUAL TABLE IF NOT EXISTS vec_memory USING vec0(
    embedding float[768]
);

-- Project registry: tracks which projects memories are affiliated with
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    path TEXT NOT NULL UNIQUE,
    tech_stack TEXT,
    domain TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

-- Junction: which memories belong to which projects
CREATE TABLE IF NOT EXISTS item_projects (
    item_type TEXT NOT NULL,
    item_id INTEGER NOT NULL,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    affinity TEXT DEFAULT 'used',
    PRIMARY KEY (item_type, item_id, project_id)
);

-- Standard B-Tree Indexes for performance
CREATE INDEX IF NOT EXISTS idx_item_tags_tag_id ON item_tags(tag_id);
CREATE INDEX IF NOT EXISTS idx_mistakes_date ON mistakes(date);
CREATE INDEX IF NOT EXISTS idx_conversations_date ON conversations(date);
CREATE INDEX IF NOT EXISTS idx_skills_domain ON skills(domain);
CREATE INDEX IF NOT EXISTS idx_prompts_domain ON prompts(domain);
CREATE INDEX IF NOT EXISTS idx_item_projects_project ON item_projects(project_id);
"""


def get_db_path():
    """Return the resolved database path."""
    return DB_PATH


@contextmanager
def get_connection(db_path=None):
    """Context manager for database connections with WAL mode and foreign keys."""
    path = db_path or DB_PATH
    db_dir = os.path.dirname(path)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")

    # Load vector extension if available
    if sqlite_vec is not None:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)

    try:
        # Dynamic Initialization & Migrations
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_meta'"
        )
        if not cursor.fetchone():
            # Brand new database setup
            conn.executescript(SCHEMA_SQL)
            conn.execute(
                "INSERT INTO schema_meta (key, value) VALUES ('version', ?)", (str(SCHEMA_VERSION),)
            )
        else:
            # Check for migrations
            existing = conn.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone()
            if existing:
                current_version = int(existing["value"])
                if current_version < SCHEMA_VERSION:
                    run_migrations(conn, current_version, SCHEMA_VERSION)

        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path=None):
    """Create tables, FTS index, and run migrations if needed."""
    with get_connection(db_path) as conn:
        conn.executescript(SCHEMA_SQL)
        # Set schema version if not present
        existing = conn.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone()

        if not existing:
            # Brand new database setup
            conn.execute(
                "INSERT INTO schema_meta (key, value) VALUES ('version', ?)",
                (str(SCHEMA_VERSION),),
            )
        else:
            # Existing database, check for migrations
            current_version = int(existing["value"])
            if current_version < SCHEMA_VERSION:
                run_migrations(conn, current_version, SCHEMA_VERSION)

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
    """Insert or replace an item in the FTS index and generate its embedding."""
    # Remove old entry if exists
    conn.execute(
        "DELETE FROM memory_fts WHERE item_type = ? AND item_id = ?",
        (item_type, str(item_id)),
    )
    tags_str = " ".join(tags_list) if tags_list else ""
    cursor = conn.execute(
        "INSERT INTO memory_fts (item_type, item_id, title, content, tags) VALUES (?, ?, ?, ?, ?)",
        (item_type, str(item_id), title, content, tags_str),
    )
    rowid = cursor.lastrowid

    # Generate and store embedding
    full_text = f"{title}\n{content}\n{tags_str}"
    embedding = embed_text(full_text)

    if embedding and sqlite_vec is not None:
        conn.execute("DELETE FROM vec_memory WHERE rowid = ?", (rowid,))
        conn.execute(
            "INSERT INTO vec_memory(rowid, embedding) VALUES (?, ?)", (rowid, json.dumps(embedding))
        )


def get_item(item_type, item_id, db_path=None):
    """Fetch the full structured data for a specific item, including its tags."""
    table_map = {
        "mistake": "mistakes",
        "pattern": "patterns",
        "skill": "skills",
        "conversation": "conversations",
        "prompt": "prompts",
    }
    table = table_map.get(item_type)
    if not table:
        return None

    with get_connection(db_path) as conn:
        row = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (item_id,)).fetchone()
        if not row:
            return None

        result = dict(row)

        # Fetch tags
        tags = conn.execute(
            "SELECT t.name FROM tags t JOIN item_tags it ON t.id = it.tag_id WHERE it.item_type = ? AND it.item_id = ?",
            (item_type, item_id),
        ).fetchall()
        result["tags"] = [t["name"] for t in tags]
        return result


def record_usage(item_type, item_id, success=True, db_path=None):
    """Increment usage count for a memory item."""
    table_map = {
        "mistake": "mistakes",
        "pattern": "patterns",
        "skill": "skills",
        "conversation": "conversations",
        "prompt": "prompts",
    }
    table = table_map.get(item_type)
    if not table:
        return False

    with get_connection(db_path) as conn:
        conn.execute(
            f"UPDATE {table} SET usage_count = usage_count + 1, last_used_at = datetime('now') WHERE id = ?",
            (item_id,),
        )
    return True


def get_or_create_project(project_path, name=None, db_path=None):
    """Get or create a project entry from its filesystem path.
    Uses the git root if available, otherwise the given path.
    """
    import subprocess

    # Try to resolve to git root for consistent project identity
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=project_path,
            capture_output=True,
            text=True,
            timeout=3,
        )
        if result.returncode == 0:
            project_path = result.stdout.strip()
    except Exception:
        pass

    # Derive name from path basename if not provided
    if not name:
        name = os.path.basename(project_path)

    with get_connection(db_path) as conn:
        row = conn.execute("SELECT * FROM projects WHERE path = ?", (project_path,)).fetchone()
        if row:
            return dict(row)
        cursor = conn.execute(
            "INSERT INTO projects (name, path) VALUES (?, ?)", (name, project_path)
        )
        return {"id": cursor.lastrowid, "name": name, "path": project_path}


def link_item_to_project(item_type, item_id, project_id, affinity="used", db_path=None):
    """Associate a memory item with a project."""
    with get_connection(db_path) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO item_projects (item_type, item_id, project_id, affinity) VALUES (?, ?, ?, ?)",
            (item_type, int(item_id), project_id, affinity),
        )
    return True


def get_project_affinities(results, project_id, db_path=None):
    """Batch fetch project affinities for a list of search results.
    Returns dict of (item_type, item_id) -> affinity string.
    """
    if not results or not project_id:
        return {}

    affinities = {}
    with get_connection(db_path) as conn:
        for r in results:
            row = conn.execute(
                "SELECT affinity FROM item_projects WHERE item_type = ? AND item_id = ? AND project_id = ?",
                (r["item_type"], int(r["item_id"]), project_id),
            ).fetchone()
            if row:
                affinities[(r["item_type"], int(r["item_id"]))] = row["affinity"]
    return affinities


def delete_item(conn, item_type, item_id):
    """Deeply delete an item from its core table, tags, FTS, and vector index."""
    # 1. Delete from core table
    tables = {
        "mistake": "mistakes",
        "pattern": "patterns",
        "skill": "skills",
        "conversation": "conversations",
        "prompt": "prompts",
    }
    table = tables.get(item_type)
    if not table:
        raise ValueError(f"Unknown item type: {item_type}")

    conn.execute(f"DELETE FROM {table} WHERE id = ?", (item_id,))

    # 2. Delete tags
    conn.execute("DELETE FROM item_tags WHERE item_type = ? AND item_id = ?", (item_type, item_id))

    # 3. Find FTS rowid to delete from vec_memory, then delete from FTS
    row = conn.execute(
        "SELECT rowid FROM memory_fts WHERE item_type = ? AND item_id = ?",
        (item_type, str(item_id)),
    ).fetchone()
    if row:
        rowid = row["rowid"]
        conn.execute("DELETE FROM memory_fts WHERE rowid = ?", (rowid,))
        if sqlite_vec is not None:
            conn.execute("DELETE FROM vec_memory WHERE rowid = ?", (rowid,))
