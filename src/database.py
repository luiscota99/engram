"""
Database module — schema, connection, and migrations for Engram.

Uses SQLite with FTS5 (bundled via sqlean-py when installed), optional sqlite-vec
for embeddings, and optional local Ollama for embedding generation — see src/embeddings.py.
"""

from __future__ import annotations

import json
import logging
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

from .embeddings import (
    embed_text,
    embedding_matches_vec_schema,
    resolve_embedding_model_name,
)
from .migrations import backup_before_migration, run_migrations

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = os.path.join(os.path.expanduser("~"), ".engram", "memory.db")

SCHEMA_VERSION = 9

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

-- Conversations: structured index of past sessions (Legacy, superseded by sessions)
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

-- Sessions: Committee-driven session ledgers
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    date TEXT NOT NULL,
    domain TEXT NOT NULL,
    workflow_used TEXT,
    tasks_completed TEXT,
    key_decisions TEXT,
    action_items TEXT,
    usage_count INTEGER DEFAULT 0,
    last_used_at TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

-- Session Transcripts: Subagent outputs for a session
CREATE TABLE IF NOT EXISTS session_transcripts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    timestamp TEXT DEFAULT (datetime('now'))
);

-- Roles: Subagent profiles (Facilitator, Analyst, etc)
CREATE TABLE IF NOT EXISTS roles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    charter TEXT NOT NULL,
    heuristics TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

-- Workflows: Standard operating procedures for the committee
CREATE TABLE IF NOT EXISTS workflows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL,
    steps TEXT NOT NULL,
    phases TEXT,
    phase_requirements TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
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

-- Codebase Knowledge: file-level summaries and structural metadata
CREATE TABLE IF NOT EXISTS codebase_knowledge (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    file_path TEXT NOT NULL,
    file_hash TEXT NOT NULL,
    summary TEXT NOT NULL,
    exports TEXT, -- JSON array of functions/classes
    dependencies TEXT, -- JSON array of imports/dependencies
    last_indexed_at TEXT DEFAULT (datetime('now')),
    UNIQUE(project_id, file_path)
);

-- Per-item embedding status for model migration visibility
CREATE TABLE IF NOT EXISTS embedding_status (
    fts_rowid INTEGER PRIMARY KEY,
    item_type TEXT NOT NULL,
    item_id INTEGER NOT NULL,
    embedding_model TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now')),
    error_message TEXT
);

-- Session state machine for workflow enforcement
CREATE TABLE IF NOT EXISTS session_state (
    session_id TEXT PRIMARY KEY REFERENCES sessions(session_id) ON DELETE CASCADE,
    current_phase TEXT NOT NULL DEFAULT 'analysis',
    required_roles TEXT NOT NULL DEFAULT '[]',
    completed_roles TEXT NOT NULL DEFAULT '[]',
    can_proceed INTEGER NOT NULL DEFAULT 0
);

-- Archive table for soft-deleted memories (GC with --archive mode)
CREATE TABLE IF NOT EXISTS archived_memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_type TEXT NOT NULL,
    item_id INTEGER NOT NULL,
    original_table TEXT NOT NULL,
    data TEXT NOT NULL,
    archived_at TEXT DEFAULT (datetime('now')),
    archive_reason TEXT
);

-- Cross-file relationship graph for codebase knowledge
CREATE TABLE IF NOT EXISTS file_relationships (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    source_file TEXT NOT NULL,
    target_file TEXT NOT NULL,
    relationship_type TEXT NOT NULL,
    UNIQUE(project_id, source_file, target_file, relationship_type)
);

-- Standard B-Tree Indexes for performance
CREATE INDEX IF NOT EXISTS idx_item_tags_tag_id ON item_tags(tag_id);
CREATE INDEX IF NOT EXISTS idx_mistakes_date ON mistakes(date);
CREATE INDEX IF NOT EXISTS idx_conversations_date ON conversations(date);
CREATE INDEX IF NOT EXISTS idx_sessions_date ON sessions(date);
CREATE INDEX IF NOT EXISTS idx_session_transcripts_session ON session_transcripts(session_id);
CREATE INDEX IF NOT EXISTS idx_skills_domain ON skills(domain);
CREATE INDEX IF NOT EXISTS idx_prompts_domain ON prompts(domain);
CREATE INDEX IF NOT EXISTS idx_item_projects_project ON item_projects(project_id);
CREATE INDEX IF NOT EXISTS idx_codebase_knowledge_project ON codebase_knowledge(project_id);
CREATE INDEX IF NOT EXISTS idx_embedding_status_status ON embedding_status(status);
CREATE INDEX IF NOT EXISTS idx_embedding_status_item ON embedding_status(item_type, item_id);
CREATE INDEX IF NOT EXISTS idx_archived_memories_type ON archived_memories(item_type);
CREATE INDEX IF NOT EXISTS idx_file_relationships_project ON file_relationships(project_id);
CREATE INDEX IF NOT EXISTS idx_file_relationships_source ON file_relationships(project_id, source_file);
CREATE INDEX IF NOT EXISTS idx_file_relationships_target ON file_relationships(project_id, target_file);
"""


def get_db_path() -> str:
    """Return the database path from ENGRAM_DB_PATH or the default ``~/.engram/memory.db``.

    Resolved at call time so tests and tooling can set ENGRAM_DB_PATH without import-order bugs.
    """
    return os.environ.get("ENGRAM_DB_PATH", DEFAULT_DB_PATH)


@contextmanager
def get_connection(db_path=None):
    """Context manager for database connections with WAL mode and foreign keys."""
    path = db_path or get_db_path()
    db_dir = os.path.dirname(path)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    # sqlean-py may lack complete stubs; runtime matches stdlib sqlite3 API.
    conn = sqlite3.connect(path)  # type: ignore[attr-defined]
    conn.row_factory = sqlite3.Row  # type: ignore[attr-defined]
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
                    # Backup before any upgrade
                    backup_path = backup_before_migration(path, current_version + 1)
                    if backup_path:
                        print(f"  ✓ Pre-migration backup saved to {backup_path}")
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
    embedding_model = resolve_embedding_model_name()

    # Remove old FTS and embedding_status entries
    old_row = conn.execute(
        "SELECT rowid FROM memory_fts WHERE item_type = ? AND item_id = ?",
        (item_type, str(item_id)),
    ).fetchone()
    if old_row:
        old_rowid = old_row["rowid"]
        conn.execute("DELETE FROM memory_fts WHERE rowid = ?", (old_rowid,))
        conn.execute("DELETE FROM embedding_status WHERE fts_rowid = ?", (old_rowid,))
        if sqlite_vec is not None:
            conn.execute("DELETE FROM vec_memory WHERE rowid = ?", (old_rowid,))

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
        ok_vec, vec_err = embedding_matches_vec_schema(embedding, embedding_model)
        if ok_vec:
            conn.execute(
                "INSERT INTO vec_memory(rowid, embedding) VALUES (?, ?)",
                (rowid, json.dumps(embedding)),
            )
            conn.execute(
                """INSERT INTO embedding_status (fts_rowid, item_type, item_id, embedding_model, status, updated_at)
                   VALUES (?, ?, ?, ?, 'ready', datetime('now'))
                   ON CONFLICT(fts_rowid) DO UPDATE SET
                     embedding_model = excluded.embedding_model,
                     status = 'ready',
                     updated_at = datetime('now'),
                     error_message = NULL""",
                (rowid, item_type, int(item_id), embedding_model),
            )
        else:
            logger.warning(
                "vec_memory skipped for %s id=%s: %s",
                item_type,
                item_id,
                vec_err,
            )
            conn.execute(
                """INSERT INTO embedding_status (fts_rowid, item_type, item_id, embedding_model, status, updated_at, error_message)
                   VALUES (?, ?, ?, ?, 'failed', datetime('now'), ?)
                   ON CONFLICT(fts_rowid) DO UPDATE SET
                     embedding_model = excluded.embedding_model,
                     status = 'failed',
                     updated_at = datetime('now'),
                     error_message = excluded.error_message""",
                (rowid, item_type, int(item_id), embedding_model, vec_err),
            )
    else:
        # Mark as pending if Ollama is not available or sqlite_vec not loaded
        status = "failed" if sqlite_vec is None else "pending"
        conn.execute(
            """INSERT INTO embedding_status (fts_rowid, item_type, item_id, embedding_model, status, updated_at)
               VALUES (?, ?, ?, ?, ?, datetime('now'))
               ON CONFLICT(fts_rowid) DO UPDATE SET
                 status = excluded.status,
                 updated_at = datetime('now')""",
            (rowid, item_type, int(item_id), embedding_model, status),
        )


def rebuild_fts(conn):
    """Fully rebuild the FTS index from core tables.

    Clears all existing FTS rows and re-inserts from mistakes, patterns,
    skills, conversations, prompts, and sessions. Also regenerates
    embeddings for any FTS row that lacks a vec_memory entry.
    Call this from `doctor --repair` or as part of migration v6.
    """
    # Clear entire FTS index (and cascade-delete vec_memory via shared rowids)
    conn.execute("DELETE FROM memory_fts")
    conn.execute("DELETE FROM vec_memory")

    rebuild_specs = [
        (
            "mistake", "mistakes",
            "mistake",
            "context || ' | ' || mistake || ' | ' || COALESCE(root_cause,'') || ' | ' || fix",
        ),
        (
            "pattern", "patterns",
            "name",
            "symptoms || ' | ' || root_cause || ' | ' || standard_fix",
        ),
        (
            "skill", "skills",
            "name",
            "trigger_desc || ' | ' || workflow || ' | ' || COALESCE(pitfalls,'')",
        ),
        (
            "conversation", "conversations",
            "title",
            "COALESCE(tasks_completed,'') || ' | ' || COALESCE(key_decisions,'')",
        ),
        (
            "prompt", "prompts",
            "name",
            "role || ' | ' || description || ' | ' || COALESCE(best_for,'')",
        ),
        (
            "session", "sessions",
            "session_id",
            "title || ' | ' || COALESCE(workflow_used,'')",
        ),
    ]

    for item_type, table, title_col, content_expr in rebuild_specs:
        rows = conn.execute(
            f"""SELECT id, {title_col} as title, {content_expr} as content
                FROM {table}"""
        ).fetchall()
        for row in rows:
            # Fetch tags for this item
            tag_rows = conn.execute(
                """SELECT t.name FROM tags t
                   JOIN item_tags it ON t.id = it.tag_id
                   WHERE it.item_type = ? AND it.item_id = ?""",
                (item_type, row["id"]),
            ).fetchall()
            tags_str = " ".join(t["name"] for t in tag_rows)
            index_in_fts(conn, item_type, row["id"], row["title"], row["content"], tags_str.split())

    return True


def get_item(item_type, item_id, db_path=None):
    """Fetch the full structured data for a specific item, including its tags."""
    table_map = {
        "mistake": "mistakes",
        "pattern": "patterns",
        "skill": "skills",
        "conversation": "conversations",
        "session": "sessions",
        "role": "roles",
        "workflow": "workflows",
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


def get_session_details(session_id, db_path=None):
    """Fetch a session by its string ID, including all transcripts and decisions."""
    with get_connection(db_path) as conn:
        row = conn.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
        if not row:
            return None

        session_data = dict(row)

        # Fetch transcripts
        transcripts = conn.execute(
            "SELECT role, content, timestamp FROM session_transcripts WHERE session_id = ? ORDER BY id ASC",
            (session_id,)
        ).fetchall()

        session_data["transcripts"] = [dict(t) for t in transcripts]
        return session_data


def record_usage(item_type, item_id, success=True, db_path=None):
    """Increment usage count for a memory item."""
    table_map = {
        "mistake": "mistakes",
        "pattern": "patterns",
        "skill": "skills",
        "conversation": "conversations",
        "session": "sessions",
        "role": "roles",
        "workflow": "workflows",
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

    # Build a single query using a VALUES clause to avoid N+1 queries
    params = []
    value_rows = []
    for r in results:
        value_rows.append("(?, ?, ?)")
        params.extend([r["item_type"], int(r["item_id"]), project_id])

    affinities = {}
    with get_connection(db_path) as conn:
        rows = conn.execute(
            f"""SELECT item_type, item_id, affinity
                FROM item_projects
                WHERE (item_type, item_id, project_id) IN (VALUES {','.join(value_rows)})""",
            params,
        ).fetchall()
        for row in rows:
            affinities[(row["item_type"], int(row["item_id"]))] = row["affinity"]
    return affinities


def find_similar(content: str, item_type: str | None = None, threshold: float = 0.85,
                 limit: int = 5, db_path=None) -> list[dict]:
    """Find memory items similar to the given content using vector search.

    Returns a list of dicts with keys: item_type, item_id, title, snippet, distance.
    Only works when sqlite_vec is available and Ollama is running.
    Returns [] gracefully if either is unavailable.
    """
    from .embeddings import embed_text as _embed

    embedding = _embed(content)
    if not embedding or sqlite_vec is None:
        return []

    model = resolve_embedding_model_name()
    ok_vec, vec_err = embedding_matches_vec_schema(embedding, model)
    if not ok_vec:
        logger.warning("find_similar: %s", vec_err)
        return []

    with get_connection(db_path) as conn:
        try:
            conditions = []
            params = []
            if item_type:
                conditions.append("f.item_type = ?")
                params.append(item_type)

            where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
            sql = f"""
                WITH matches AS (
                    SELECT rowid, distance
                    FROM vec_memory
                    WHERE embedding MATCH ? AND k = ?
                )
                SELECT f.item_type, f.item_id, f.title, f.content AS snippet,
                       m.distance,
                       (1.0 - m.distance) AS similarity
                FROM matches m
                JOIN memory_fts f ON m.rowid = f.rowid
                {where}
                ORDER BY m.distance
                LIMIT ?
            """
            rows = conn.execute(
                sql, [json.dumps(embedding), limit * 3] + params + [limit]
            ).fetchall()
            results = []
            for row in rows:
                sim = row["similarity"]
                if sim >= threshold:
                    results.append({
                        "item_type": row["item_type"],
                        "item_id": row["item_id"],
                        "title": row["title"],
                        "snippet": (row["snippet"] or "")[:200],
                        "distance": row["distance"],
                        "similarity": round(sim, 4),
                    })
            return results
        except Exception:
            logger.exception("find_similar vector query failed")
            return []


def get_embedding_stats(db_path=None) -> dict:
    """Return counts of embedding_status values plus the current model."""
    current_model = resolve_embedding_model_name()

    with get_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) as cnt FROM embedding_status GROUP BY status"
        ).fetchall()
        counts = {r["status"]: r["cnt"] for r in rows}
        total = sum(counts.values())
        return {
            "model": current_model,
            "total": total,
            "ready": counts.get("ready", 0),
            "stale": counts.get("stale", 0),
            "pending": counts.get("pending", 0),
            "failed": counts.get("failed", 0),
        }


def mark_embeddings_stale(db_path=None) -> int:
    """Mark all 'ready' embeddings as 'stale' (call when switching embedding models).

    Returns the count of rows updated.
    """
    with get_connection(db_path) as conn:
        cursor = conn.execute(
            "UPDATE embedding_status SET status = 'stale', updated_at = datetime('now') "
            "WHERE status = 'ready'"
        )
        return cursor.rowcount


def reembed_stale(db_path=None, batch_size: int = 50) -> dict:
    """Re-generate embeddings for all stale/pending items.

    Processes up to batch_size items per call.  Returns a progress dict:
      {'processed': N, 'succeeded': N, 'failed': N, 'remaining': N}
    """
    from .embeddings import embed_text as _embed

    if sqlite_vec is None:
        return {"processed": 0, "succeeded": 0, "failed": 0, "remaining": 0, "error": "sqlite_vec not available"}

    embedding_model = resolve_embedding_model_name()
    succeeded = failed = 0

    with get_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT fts_rowid, item_type, item_id FROM embedding_status "
            "WHERE status IN ('stale', 'pending') LIMIT ?",
            (batch_size,)
        ).fetchall()

        for row in rows:
            fts_row = conn.execute(
                "SELECT title, content, tags FROM memory_fts WHERE rowid = ?",
                (row["fts_rowid"],)
            ).fetchone()
            if not fts_row:
                conn.execute(
                    "UPDATE embedding_status SET status = 'failed', error_message = 'FTS row missing', "
                    "updated_at = datetime('now') WHERE fts_rowid = ?",
                    (row["fts_rowid"],)
                )
                failed += 1
                continue

            full_text = f"{fts_row['title']}\n{fts_row['content']}\n{fts_row['tags'] or ''}"
            embedding = _embed(full_text, model=embedding_model)

            if embedding:
                ok_vec, vec_err = embedding_matches_vec_schema(embedding, embedding_model)
                if ok_vec:
                    conn.execute("DELETE FROM vec_memory WHERE rowid = ?", (row["fts_rowid"],))
                    conn.execute(
                        "INSERT INTO vec_memory(rowid, embedding) VALUES (?, ?)",
                        (row["fts_rowid"], json.dumps(embedding)),
                    )
                    conn.execute(
                        "UPDATE embedding_status SET status = 'ready', embedding_model = ?, "
                        "updated_at = datetime('now'), error_message = NULL WHERE fts_rowid = ?",
                        (embedding_model, row["fts_rowid"]),
                    )
                    succeeded += 1
                else:
                    logger.warning("reembed_stale skipping vec for fts_rowid=%s: %s", row["fts_rowid"], vec_err)
                    conn.execute(
                        "UPDATE embedding_status SET status = 'failed', error_message = ?, "
                        "updated_at = datetime('now') WHERE fts_rowid = ?",
                        (vec_err, row["fts_rowid"]),
                    )
                    failed += 1
            else:
                conn.execute(
                    "UPDATE embedding_status SET status = 'failed', "
                    "error_message = 'Ollama unavailable or model not found', "
                    "updated_at = datetime('now') WHERE fts_rowid = ?",
                    (row["fts_rowid"],),
                )
                failed += 1

        remaining = conn.execute(
            "SELECT COUNT(*) as c FROM embedding_status WHERE status IN ('stale', 'pending')"
        ).fetchone()["c"]

    return {
        "processed": len(rows),
        "succeeded": succeeded,
        "failed": failed,
        "remaining": remaining,
    }


def delete_item(conn, item_type, item_id):
    """Deeply delete an item from its core table, tags, FTS, and vector index."""
    # 1. Delete from core table
    tables = {
        "mistake": "mistakes",
        "pattern": "patterns",
        "skill": "skills",
        "conversation": "conversations",
        "session": "sessions",
        "role": "roles",
        "workflow": "workflows",
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
        conn.execute("DELETE FROM embedding_status WHERE fts_rowid = ?", (rowid,))
