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

from . import config
from .embeddings import (
    embed_batch,
    embed_text,
    embedding_matches_vec_schema,
    resolve_embedding_model_name,
)
from .item_registry import dedup_table_map, rebuild_specs, table_for
from .migrations import backup_before_migration, run_migrations

logger = logging.getLogger(__name__)

# Warn once per process when the vec extension can't load (see get_connection).
_vec_load_warned = False

DEFAULT_DB_PATH = os.path.join(os.path.expanduser("~"), ".engram", "memory.db")

SCHEMA_VERSION = 26

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
    created_at TEXT DEFAULT (datetime('now')),
    superseded_by INTEGER
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
    created_at TEXT DEFAULT (datetime('now')),
    superseded_by INTEGER
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
    updated_at TEXT DEFAULT (datetime('now')),
    superseded_by INTEGER
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

-- Full-text search index across all memory types.
-- porter stemming: 'committing' matches 'commit', 'verification' matches
-- 'verify' — without it, natural phrasings missed morphological variants.
CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
    item_type,
    item_id UNINDEXED,
    title,
    content,
    tags,
    tokenize='porter unicode61'
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
    file_mtime REAL,
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

-- Pinned items: always prepended to search results (core facts)
CREATE TABLE IF NOT EXISTS item_pins (
    item_type TEXT NOT NULL,
    item_id INTEGER NOT NULL,
    pinned_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (item_type, item_id)
);
CREATE INDEX IF NOT EXISTS idx_item_pins_type ON item_pins(item_type);

-- Fingerprint cache to skip redundant consolidation scans
CREATE TABLE IF NOT EXISTS consolidation_state (
    key TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    updated_at TEXT DEFAULT (datetime('now'))
);

-- Reflexes: skills promoted to executable, human-approved scripts (schema v13)
-- A reflex is a proven workflow compiled to a script: agents invoke it as an
-- MCP tool instead of re-reasoning through the workflow text each time.
CREATE TABLE IF NOT EXISTS reflexes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    skill_id INTEGER NOT NULL REFERENCES skills(id) ON DELETE CASCADE,
    name TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL,
    script TEXT NOT NULL,
    interpreter TEXT NOT NULL DEFAULT 'bash',
    params_schema TEXT,
    approved_at TEXT,
    approved_hash TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    run_count INTEGER DEFAULT 0,
    last_run_at TEXT,
    last_status TEXT,
    fail_streak INTEGER DEFAULT 0,
    kind TEXT NOT NULL DEFAULT 'action',
    read_only INTEGER NOT NULL DEFAULT 0
);

-- Per-run reflex execution history (schema v15): success *rates*, not
-- just streaks, so promotion/demotion decisions rest on real distributions.
CREATE TABLE IF NOT EXISTS reflex_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    reflex_id INTEGER NOT NULL REFERENCES reflexes(id) ON DELETE CASCADE,
    started_at TEXT NOT NULL,
    duration_ms INTEGER,
    status TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reflex_runs_reflex ON reflex_runs(reflex_id);

-- Skill validation tests (schema v19): a scenario a memory must PASS with
-- and FAIL without — proving it actually changes behavior, not just that it's
-- stored (Superpowers' TDD-for-skills rigor applied to personal memory).
CREATE TABLE IF NOT EXISTS skill_tests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_type TEXT NOT NULL,
    item_id INTEGER NOT NULL,
    scenario TEXT NOT NULL,
    assertion TEXT NOT NULL,
    grader TEXT NOT NULL DEFAULT 'contains',   -- contains | llm_judge
    last_result TEXT,                          -- validated | redundant | ineffective | untested
    baseline_passed INTEGER,
    treatment_passed INTEGER,
    last_run_at TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_skill_tests_item ON skill_tests(item_type, item_id);

-- Typed relationships between memory items (schema v20). Small closed vocabulary
-- of edge types; source = manual | merge (auto-derived).
CREATE TABLE IF NOT EXISTS memory_relations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_type TEXT NOT NULL,
    from_id INTEGER NOT NULL,
    to_type TEXT NOT NULL,
    to_id INTEGER NOT NULL,
    relation TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'manual',
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(from_type, from_id, to_type, to_id, relation)
);
CREATE INDEX IF NOT EXISTS idx_relations_from ON memory_relations(from_type, from_id);
CREATE INDEX IF NOT EXISTS idx_relations_to ON memory_relations(to_type, to_id);

-- Crash-proof session checkpoints (schema v21) — one row per (project, session),
-- upserted by the Stop hook every agent turn. Operational state, not a memory
-- item type: never FTS-indexed or embedded; read only via `engram resume`.
CREATE TABLE IF NOT EXISTS checkpoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_path TEXT NOT NULL,
    session_id TEXT NOT NULL,
    last_prompt TEXT NOT NULL DEFAULT '',
    last_summary TEXT NOT NULL DEFAULT '',
    git_head TEXT NOT NULL DEFAULT '',
    git_branch TEXT NOT NULL DEFAULT '',
    turn_count INTEGER NOT NULL DEFAULT 0,
    milestone_summary TEXT,                        -- deliberate handoff (v26)
    milestone_at TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now')),
    UNIQUE(project_path, session_id)
);
CREATE INDEX IF NOT EXISTS idx_checkpoints_project ON checkpoints(project_path, updated_at);

-- Retrieval feedback (schema v22) — reward/discourage signal for RANKING only.
-- helpful=+1 rewards, helpful=-1 demotes; never deletes (the user decides that,
-- via inbox proposals). One table for all item types: rank-time aggregation is
-- a single batch query.
CREATE TABLE IF NOT EXISTS retrieval_feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_type TEXT NOT NULL,
    item_id INTEGER NOT NULL,
    helpful INTEGER NOT NULL CHECK (helpful IN (1, -1)),
    query TEXT,
    source TEXT NOT NULL DEFAULT 'manual',
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_feedback_item ON retrieval_feedback(item_type, item_id);

-- Per-memory forgetting curves (schema v25, FSRS-4.5). Evolved by usage and
-- feedback events; items without a row keep fixed-half-life ranking behavior.
CREATE TABLE IF NOT EXISTS memory_dynamics (
    item_type TEXT NOT NULL,
    item_id INTEGER NOT NULL,
    stability REAL NOT NULL,
    difficulty REAL NOT NULL DEFAULT 5.0,
    last_event_at TEXT NOT NULL DEFAULT (datetime('now')),
    reps INTEGER NOT NULL DEFAULT 0,
    lapses INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (item_type, item_id)
);

-- Inbox: alerts and decision requests for the human (schema v17).
-- Agents and monitors PROPOSE here; only the user decides. finding_key
-- dedups recurring findings (daily self-check must not re-file open items).
CREATE TABLE IF NOT EXISTS inbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL DEFAULT 'alert',            -- alert | decision
    severity TEXT NOT NULL DEFAULT 'warning',      -- info|warning|high|critical
    title TEXT NOT NULL,
    body TEXT,
    source TEXT,
    finding_key TEXT,
    proposed_reflex_id INTEGER REFERENCES reflexes(id) ON DELETE SET NULL,
    proposed_params TEXT,
    status TEXT NOT NULL DEFAULT 'open',           -- open|acknowledged|approved|rejected|executed
    created_at TEXT DEFAULT (datetime('now')),
    decided_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_inbox_status ON inbox(status);
-- Race-proof dedup (v24): finding_key unique among OPEN items only.
CREATE UNIQUE INDEX IF NOT EXISTS idx_inbox_open_finding
    ON inbox(finding_key) WHERE status = 'open' AND finding_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_inbox_finding ON inbox(finding_key);

-- Change journal: every mutation a reflex reports (schema v17). Scripts emit
-- `ENGRAM_CHANGE target=... before=... after=...` lines; run_reflex journals
-- them so any change is revertible-by-information.
CREATE TABLE IF NOT EXISTS reflex_changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    reflex_run_id INTEGER REFERENCES reflex_runs(id) ON DELETE CASCADE,
    target TEXT NOT NULL,
    before_value TEXT,
    after_value TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

-- Temporal facts: supersession/invalidation history (schema v11)
CREATE TABLE IF NOT EXISTS memory_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject TEXT NOT NULL,
    predicate TEXT NOT NULL,
    object TEXT NOT NULL,
    valid_from TEXT NOT NULL DEFAULT (date('now')),
    valid_until TEXT,
    source_type TEXT,
    source_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_memory_facts_subject ON memory_facts(subject);
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
    # Hooks made engram multi-process (recall + guard + checkpoint can fire
    # near-simultaneously from several sessions); wait out writers instead of
    # failing fast with "database is locked".
    conn.execute("PRAGMA busy_timeout=10000")

    # Load vector extension if available. A load failure (e.g. macOS TCC
    # blocking the dylib after a permissions change) must degrade to
    # lexical-only search, not kill every connection.
    if sqlite_vec is not None:
        try:
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)
        except Exception:
            global _vec_load_warned
            if not _vec_load_warned:
                _vec_load_warned = True
                logger.warning(
                    "sqlite-vec extension failed to load — semantic search disabled "
                    "for this process; lexical search continues",
                    exc_info=True,
                )

    try:
        # Dynamic Initialization & Migrations
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_meta'"
        )
        if not cursor.fetchone():
            # Brand new database setup. OR IGNORE: two processes can race the
            # first-ever connection; the schema is IF NOT EXISTS throughout.
            conn.executescript(SCHEMA_SQL)
            conn.execute(
                "INSERT OR IGNORE INTO schema_meta (key, value) VALUES ('version', ?)",
                (str(SCHEMA_VERSION),),
            )
        else:
            # Check for migrations
            existing = conn.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone()
            if existing:
                current_version = int(existing["value"])
                if current_version < SCHEMA_VERSION:
                    # Serialize migrators: concurrent hooks all reach this
                    # branch after an upgrade; without the write lock two
                    # processes can interleave a multi-step migration (v16's
                    # FTS swap mid-DROP). BEGIN IMMEDIATE takes the lock, then
                    # re-check — the loser sees the bumped version and skips.
                    conn.execute("BEGIN IMMEDIATE")
                    row = conn.execute(
                        "SELECT value FROM schema_meta WHERE key='version'"
                    ).fetchone()
                    current_version = int(row["value"]) if row else current_version
                    if current_version < SCHEMA_VERSION:
                        backup_path = backup_before_migration(path, current_version + 1)
                        if backup_path:
                            logger.info("Pre-migration backup saved to %s", backup_path)
                        run_migrations(conn, current_version, SCHEMA_VERSION)
                    conn.commit()

        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def connection_scope(conn=None, db_path=None):
    """Yield *conn* when the caller already holds one (caller keeps ownership of
    commit/close), otherwise open a fresh ``get_connection``.

    Lets multi-step operations share a single connection instead of paying the
    per-connection setup (PRAGMAs, extension load, schema check) several times.
    """
    if conn is not None:
        yield conn
    else:
        with get_connection(db_path) as fresh:
            yield fresh


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
    """Get or create a tag, return its id.

    Upsert-shaped (INSERT OR IGNORE, then SELECT) so two hook processes
    racing on the same new tag can't abort a capture with IntegrityError.
    """
    tag_name = tag_name.strip().lower()
    conn.execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", (tag_name,))
    return conn.execute("SELECT id FROM tags WHERE name = ?", (tag_name,)).fetchone()["id"]


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

    # Generate and store embedding (skipped in deferred mode → row goes to
    # 'pending' below and a batched `engram reembed` sweep picks it up)
    full_text = f"{title}\n{content}\n{tags_str}"
    embedding = None if config.defer_embed() else embed_text(full_text)

    if embedding and sqlite_vec is not None:
        ok_vec, vec_err = embedding_matches_vec_schema(
            embedding, embedding_model, expected_dim=get_vec_dimension(conn=conn)
        )
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

    rebuild_specs_list = rebuild_specs()
    for item_type, table, title_col, content_expr in rebuild_specs_list:
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
    table = table_for(item_type)
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
    table = table_for(item_type)
    if not table:
        return False

    with get_connection(db_path) as conn:
        conn.execute(
            f"UPDATE {table} SET usage_count = usage_count + 1, last_used_at = datetime('now') WHERE id = ?",
            (item_id,),
        )
        # A use is a successful recall: grow the item's forgetting-curve
        # stability (FSRS rating "good"). Never raises.
        from .stability import GOOD, record_event

        record_event(item_type, int(item_id), GOOD, conn=conn)
    return True


_git_root_cache: dict = {}


def _resolve_git_root(project_path: str) -> str:
    """Git root for a path, memoized — this fork ran on EVERY search before."""
    cached = _git_root_cache.get(project_path)
    if cached is not None:
        return cached
    import subprocess

    resolved = project_path
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=project_path,
            capture_output=True,
            text=True,
            timeout=3,
        )
        if result.returncode == 0:
            resolved = result.stdout.strip()
    except Exception:
        logger.debug("git root resolution failed for %s; using path as-is", project_path, exc_info=True)
    _git_root_cache[project_path] = resolved
    return resolved


def get_or_create_project(project_path, name=None, db_path=None, conn=None):
    """Get or create a project entry from its filesystem path.
    Uses the git root if available, otherwise the given path.
    """
    project_path = _resolve_git_root(project_path)

    # Derive name from path basename if not provided
    if not name:
        name = os.path.basename(project_path)

    with connection_scope(conn, db_path) as c:
        # Upsert-shaped for the same reason as ensure_tag: concurrent hooks
        # from two sessions can both see "no project" and race the INSERT.
        c.execute(
            "INSERT OR IGNORE INTO projects (name, path) VALUES (?, ?)", (name, project_path)
        )
        row = c.execute("SELECT * FROM projects WHERE path = ?", (project_path,)).fetchone()
        return dict(row)


def link_item_to_project(item_type, item_id, project_id, affinity="used", db_path=None):
    """Associate a memory item with a project."""
    with get_connection(db_path) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO item_projects (item_type, item_id, project_id, affinity) VALUES (?, ?, ?, ?)",
            (item_type, int(item_id), project_id, affinity),
        )
    return True


def get_project_affinities(results, project_id, db_path=None, conn=None):
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
    with connection_scope(conn, db_path) as conn:
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
    ok_vec, vec_err = embedding_matches_vec_schema(
        embedding, model, expected_dim=get_vec_dimension(db_path=db_path)
    )
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


# Documents per batched embedding request during reembed sweeps.
EMBED_SWEEP_CHUNK = 16


def reembed_stale(db_path=None, batch_size: int = 50) -> dict:
    """Re-generate embeddings for all stale/pending items.

    Processes up to batch_size items per call.  Returns a progress dict:
      {'processed': N, 'succeeded': N, 'failed': N, 'remaining': N}
    """
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

        # Resolve texts first, then embed in small sub-batches — one HTTP
        # round-trip per sub-batch instead of one per document. 16 keeps each
        # request short enough that a saturated local Ollama can't wedge a
        # whole sweep behind one giant call.
        embeddable: list[tuple] = []  # (row, full_text)
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
            embeddable.append((row, full_text))

        # Prime the model before the sweep: a cold load landing mid-batch would
        # blow the per-request timeout and trip the dead-host cooldown, failing
        # the rest of the items (observed on CPU-only Ollama).
        if embeddable:
            from . import embeddings as _emb

            _emb.warm_up(model=embedding_model)

        embeddings: list = []
        for start in range(0, len(embeddable), EMBED_SWEEP_CHUNK):
            chunk = embeddable[start:start + EMBED_SWEEP_CHUNK]
            embeddings.extend(embed_batch([t for _r, t in chunk], model=embedding_model))

        for (row, _full_text), embedding in zip(embeddable, embeddings):
            if embedding:
                ok_vec, vec_err = embedding_matches_vec_schema(
                    embedding, embedding_model, expected_dim=get_vec_dimension(conn=conn)
                )
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


# Side tables that reference items by soft (item_type, item_id) pairs — no
# real FK can enforce these, so delete_item must clean them and
# integrity_report must watch them (July 2026 audit).
SOFT_FK_TABLES = (
    "item_projects",
    "item_pins",
    "skill_tests",
    "retrieval_feedback",
    "memory_dynamics",
)


def delete_item(conn, item_type, item_id):
    """Deeply delete an item: core table, tags, FTS, vectors, and every
    soft-FK side table (projects, pins, tests, feedback, dynamics, relations)."""
    table = table_for(item_type)
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

    # 4. Soft-FK side tables (typed relations reference items on both ends)
    for side in SOFT_FK_TABLES:
        conn.execute(
            f"DELETE FROM {side} WHERE item_type = ? AND item_id = ?",
            (item_type, int(item_id)),
        )
    conn.execute(
        "DELETE FROM memory_relations WHERE (from_type = ? AND from_id = ?) "
        "OR (to_type = ? AND to_id = ?)",
        (item_type, int(item_id), item_type, int(item_id)),
    )


PINNABLE_TYPES = frozenset({"mistake", "pattern", "skill", "conversation", "prompt"})
DEDUP_VECTOR_THRESHOLD = 0.72


def pin_item(item_type: str, item_id: int, db_path=None) -> bool:
    """Pin a memory item so it is always prepended to search results."""
    if item_type not in PINNABLE_TYPES:
        raise ValueError(f"Cannot pin item type: {item_type}")
    if not get_item(item_type, item_id, db_path=db_path):
        return False
    with get_connection(db_path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO item_pins (item_type, item_id, pinned_at) VALUES (?, ?, datetime('now'))",
            (item_type, int(item_id)),
        )
    return True


def unpin_item(item_type: str, item_id: int, db_path=None) -> bool:
    """Remove pin from a memory item."""
    with get_connection(db_path) as conn:
        cursor = conn.execute(
            "DELETE FROM item_pins WHERE item_type = ? AND item_id = ?",
            (item_type, int(item_id)),
        )
        return cursor.rowcount > 0


def is_pinned(item_type: str, item_id: int, db_path=None) -> bool:
    with get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT 1 FROM item_pins WHERE item_type = ? AND item_id = ?",
            (item_type, int(item_id)),
        ).fetchone()
        return row is not None


def get_pinned_items(item_type: str | None = None, limit: int = 20, db_path=None, conn=None) -> list[dict]:
    """Return pinned memory rows joined with FTS metadata."""
    with connection_scope(conn, db_path) as conn:
        conditions = []
        params: list = []
        if item_type:
            conditions.append("p.item_type = ?")
            params.append(item_type)
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        rows = conn.execute(
            f"""SELECT p.item_type, p.item_id, p.pinned_at,
                       f.title, f.content AS snippet, f.tags, f.rowid AS fts_rowid
                FROM item_pins p
                JOIN memory_fts f
                  ON f.item_type = p.item_type AND CAST(f.item_id AS INTEGER) = p.item_id
                {where}
                ORDER BY p.pinned_at DESC
                LIMIT ?""",
            params + [limit],
        ).fetchall()
        results = []
        for row in rows:
            results.append({
                "item_type": row["item_type"],
                "item_id": str(row["item_id"]),
                "title": row["title"],
                "snippet": row["snippet"] or "",
                "tags": row["tags"],
                "pinned": True,
                "pinned_at": row["pinned_at"],
                "rowid": row["fts_rowid"],
                "is_semantic": False,
                "utility_score": 9999.0,
            })
        return results


def _jaccard_similarity(a: str, b: str) -> float:
    ta = set(a.lower().split())
    tb = set(b.lower().split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def check_duplicate_before_add(
    content: str,
    item_type: str,
    *,
    name: str | None = None,
    threshold: float = DEDUP_VECTOR_THRESHOLD,
    db_path=None,
) -> dict:
    """Multi-layer dedup check before insert (vector → exact name → Jaccard).

    Returns dict with keys: duplicates (list), exact_match (bool), fuzzy_match (bool).
    """
    result: dict = {"duplicates": [], "exact_match": False, "fuzzy_match": False}
    if not content and not name:
        return result

    table_map = dedup_table_map()
    if name and item_type in table_map:
        table, col = table_map[item_type]
        with get_connection(db_path) as conn:
            row = conn.execute(
                f"SELECT id FROM {table} WHERE lower({col}) = lower(?)",
                (name.strip(),),
            ).fetchone()
            if row:
                result["exact_match"] = True
                result["duplicates"].append({
                    "item_type": item_type,
                    "item_id": row["id"],
                    "title": name,
                    "similarity": 1.0,
                    "match_kind": "exact_name",
                })
                return result

    similar = find_similar(content, item_type=item_type, threshold=threshold, limit=3, db_path=db_path)
    for hit in similar:
        hit["match_kind"] = "vector"
        result["duplicates"].append(hit)

    if not result["duplicates"] and content:
        with get_connection(db_path) as conn:
            rows = conn.execute(
                "SELECT item_type, item_id, title, content FROM memory_fts WHERE item_type = ?",
                (item_type,),
            ).fetchall()
            for row in rows:
                text = f"{row['title']} {row['content'] or ''}"
                if _jaccard_similarity(content, text) >= 0.6:
                    result["fuzzy_match"] = True
                    result["duplicates"].append({
                        "item_type": row["item_type"],
                        "item_id": row["item_id"],
                        "title": row["title"],
                        "similarity": round(_jaccard_similarity(content, text), 4),
                        "match_kind": "jaccard",
                    })
                    break

    return result


def get_consolidation_fingerprint(item_types: list[str] | None = None, db_path=None) -> str:
    """Stable SHA256 of id+title+type for consolidation fingerprinting."""
    import hashlib

    types = item_types or ["mistake", "pattern", "skill"]
    items: list[tuple[str, str, str]] = []
    with get_connection(db_path) as conn:
        for itype in types:
            rows = conn.execute(
                "SELECT item_id, title FROM memory_fts WHERE item_type = ? ORDER BY item_id",
                (itype,),
            ).fetchall()
            for row in rows:
                items.append((itype, str(row["item_id"]), row["title"] or ""))
    items.sort()
    h = hashlib.sha256()
    for triple in items:
        h.update(("\x1f".join(triple) + "\x1e").encode("utf-8"))
    return h.hexdigest()


def get_stored_consolidation_fingerprint(key: str = "default", db_path=None) -> str | None:
    with get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT fingerprint FROM consolidation_state WHERE key = ?",
            (key,),
        ).fetchone()
        return row["fingerprint"] if row else None


def save_consolidation_fingerprint(fingerprint: str, key: str = "default", db_path=None) -> None:
    with get_connection(db_path) as conn:
        conn.execute(
            """INSERT INTO consolidation_state (key, fingerprint, updated_at)
               VALUES (?, ?, datetime('now'))
               ON CONFLICT(key) DO UPDATE SET
                 fingerprint = excluded.fingerprint,
                 updated_at = datetime('now')""",
            (key, fingerprint),
        )


def get_schema_meta(key: str, db_path=None) -> str | None:
    with get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT value FROM schema_meta WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else None


def set_schema_meta(key: str, value: str, db_path=None) -> None:
    with get_connection(db_path) as conn:
        conn.execute(
            """INSERT INTO schema_meta (key, value) VALUES (?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
            (key, value),
        )


def get_vec_dimension(conn=None, db_path=None) -> int:
    """Live vec_memory dimension: schema_meta.vec_dimension, else the 768 default."""
    from .embeddings import VEC_EMBEDDING_DIMENSION

    try:
        with connection_scope(conn, db_path) as c:
            row = c.execute(
                "SELECT value FROM schema_meta WHERE key='vec_dimension'"
            ).fetchone()
        if row:
            return int(row["value"])
    except Exception:
        logger.debug("vec_dimension lookup failed; using default", exc_info=True)
    return VEC_EMBEDDING_DIMENSION


def rebuild_vec_table(dim: int, conn) -> None:
    """Drop and recreate vec_memory with a new embedding dimension.

    All vectors are lost — callers must mark embeddings stale and re-embed.
    """
    conn.execute("DROP TABLE IF EXISTS vec_memory")
    conn.execute(
        f"CREATE VIRTUAL TABLE vec_memory USING vec0(embedding float[{int(dim)}])"
    )


def verify_embedding_schema_match(db_path=None) -> str | None:
    """Return error message if live embedding dim != stored vec_dimension, else None."""
    from .embeddings import (
        VEC_EMBEDDING_DIMENSION,
        embed_text,
        resolve_embed_backend,
        resolve_embedding_model_name,
    )

    if resolve_embed_backend()[0] == "disabled":
        return None

    stored = get_schema_meta("vec_dimension", db_path=db_path)
    if not stored:
        set_schema_meta("vec_dimension", str(VEC_EMBEDDING_DIMENSION), db_path=db_path)
        return None

    try:
        expected = int(stored)
    except ValueError:
        return None

    probe = embed_text("dimension probe")
    if probe is None:
        return None
    if len(probe) != expected:
        model = resolve_embedding_model_name()
        return (
            f"Embedding model {model!r} produced {len(probe)}-dim vectors but "
            f"schema expects {expected}. Run: engram migrate-embeddings --target-model {model}"
        )
    return None


def migrate_embeddings_to_model(target_model: str, db_path=None) -> dict:
    """Switch the embedding model, rebuilding vec_memory if the dimension changes.

    Probes the model for its real output dimension. If it differs from the live
    vec_memory dimension, the vector table is dropped and recreated at the new
    width (all vectors are regenerated from FTS content, so nothing is lost).
    """
    from .embeddings import embed_text

    os.environ["ENGRAM_EMBED_MODEL"] = target_model
    probe = embed_text("migrate probe", model=target_model)
    if probe is None:
        return {"ok": False, "error": "Could not reach the embedding backend to probe the model."}

    new_dim = len(probe)
    current_dim = get_vec_dimension(db_path=db_path)
    rebuilt = False
    if new_dim != current_dim:
        if sqlite_vec is None:
            return {"ok": False, "error": "sqlite-vec extension unavailable; cannot rebuild vec_memory."}
        with get_connection(db_path) as conn:
            rebuild_vec_table(new_dim, conn)
        rebuilt = True

    stale_count = mark_embeddings_stale(db_path=db_path)
    set_schema_meta("embed_model", target_model, db_path=db_path)
    set_schema_meta("vec_dimension", str(new_dim), db_path=db_path)
    reembed_result = reembed_stale(db_path=db_path)
    return {
        "ok": True,
        "target_model": target_model,
        "dimension": new_dim,
        "vec_table_rebuilt": rebuilt,
        "marked_stale": stale_count,
        "reembed": reembed_result,
    }
