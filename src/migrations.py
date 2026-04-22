"""
Database migrations runner for Engram.

Each version entry in MIGRATIONS is a list of SQL statements to execute
in order.  DOWNGRADES maps each version to the SQL that reverses it.

Safe migration guarantees:
  - Before any upgrade, the caller should snapshot the DB (handled in database.py).
  - Downgrade SQL is best-effort: some ALTER TABLE adds cannot be reversed in
    SQLite; those steps are noted as no-ops (the column simply stays).
"""

from __future__ import annotations


import os
import shutil


MIGRATIONS = {
    2: ["CREATE TABLE IF NOT EXISTS _test_migration_v2 (id INTEGER PRIMARY KEY);"],
    3: [
        "ALTER TABLE mistakes ADD COLUMN usage_count INTEGER DEFAULT 0;",
        "ALTER TABLE mistakes ADD COLUMN last_used_at TEXT;",
        "ALTER TABLE patterns ADD COLUMN usage_count INTEGER DEFAULT 0;",
        "ALTER TABLE patterns ADD COLUMN last_used_at TEXT;",
        "ALTER TABLE skills ADD COLUMN usage_count INTEGER DEFAULT 0;",
        "ALTER TABLE skills ADD COLUMN last_used_at TEXT;",
        "ALTER TABLE conversations ADD COLUMN usage_count INTEGER DEFAULT 0;",
        "ALTER TABLE conversations ADD COLUMN last_used_at TEXT;",
        "ALTER TABLE prompts ADD COLUMN usage_count INTEGER DEFAULT 0;",
        "ALTER TABLE prompts ADD COLUMN last_used_at TEXT;",
    ],
    4: [
        """CREATE TABLE IF NOT EXISTS projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            path TEXT NOT NULL UNIQUE,
            tech_stack TEXT,
            domain TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );""",
        """CREATE TABLE IF NOT EXISTS item_projects (
            item_type TEXT NOT NULL,
            item_id INTEGER NOT NULL,
            project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            affinity TEXT DEFAULT 'used',
            PRIMARY KEY (item_type, item_id, project_id)
        );""",
        "CREATE INDEX IF NOT EXISTS idx_item_projects_project ON item_projects(project_id);",
    ],
    5: [
        """CREATE TABLE IF NOT EXISTS sessions (
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
        );""",
        """CREATE TABLE IF NOT EXISTS session_transcripts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            timestamp TEXT DEFAULT (datetime('now'))
        );""",
        "CREATE INDEX IF NOT EXISTS idx_session_transcripts_session ON session_transcripts(session_id);",
        """CREATE TABLE IF NOT EXISTS roles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            charter TEXT NOT NULL,
            heuristics TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        );""",
        """CREATE TABLE IF NOT EXISTS workflows (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            description TEXT NOT NULL,
            steps TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        );""",
    ],
    # v6: FTS triggers for all core tables.
    6: [
        # ── mistakes ──────────────────────────────────────────────────────
        """CREATE TRIGGER IF NOT EXISTS fts_mistakes_after_insert
           AFTER INSERT ON mistakes BEGIN
             INSERT INTO memory_fts(item_type, item_id, title, content, tags)
             VALUES ('mistake', NEW.id,
                     SUBSTR(NEW.mistake, 1, 80),
                     NEW.context || ' | ' || NEW.mistake || ' | ' || COALESCE(NEW.root_cause,'') || ' | ' || NEW.fix,
                     '');
           END;""",
        """CREATE TRIGGER IF NOT EXISTS fts_mistakes_after_delete
           AFTER DELETE ON mistakes BEGIN
             DELETE FROM memory_fts WHERE item_type = 'mistake' AND item_id = CAST(OLD.id AS TEXT);
           END;""",
        """CREATE TRIGGER IF NOT EXISTS fts_mistakes_after_update
           AFTER UPDATE ON mistakes BEGIN
             DELETE FROM memory_fts WHERE item_type = 'mistake' AND item_id = CAST(OLD.id AS TEXT);
             INSERT INTO memory_fts(item_type, item_id, title, content, tags)
             VALUES ('mistake', NEW.id,
                     SUBSTR(NEW.mistake, 1, 80),
                     NEW.context || ' | ' || NEW.mistake || ' | ' || COALESCE(NEW.root_cause,'') || ' | ' || NEW.fix,
                     '');
           END;""",
        # ── patterns ──────────────────────────────────────────────────────
        """CREATE TRIGGER IF NOT EXISTS fts_patterns_after_insert
           AFTER INSERT ON patterns BEGIN
             INSERT INTO memory_fts(item_type, item_id, title, content, tags)
             VALUES ('pattern', NEW.id, NEW.name,
                     NEW.symptoms || ' | ' || NEW.root_cause || ' | ' || NEW.standard_fix, '');
           END;""",
        """CREATE TRIGGER IF NOT EXISTS fts_patterns_after_delete
           AFTER DELETE ON patterns BEGIN
             DELETE FROM memory_fts WHERE item_type = 'pattern' AND item_id = CAST(OLD.id AS TEXT);
           END;""",
        """CREATE TRIGGER IF NOT EXISTS fts_patterns_after_update
           AFTER UPDATE ON patterns BEGIN
             DELETE FROM memory_fts WHERE item_type = 'pattern' AND item_id = CAST(OLD.id AS TEXT);
             INSERT INTO memory_fts(item_type, item_id, title, content, tags)
             VALUES ('pattern', NEW.id, NEW.name,
                     NEW.symptoms || ' | ' || NEW.root_cause || ' | ' || NEW.standard_fix, '');
           END;""",
        # ── skills ────────────────────────────────────────────────────────
        """CREATE TRIGGER IF NOT EXISTS fts_skills_after_insert
           AFTER INSERT ON skills BEGIN
             INSERT INTO memory_fts(item_type, item_id, title, content, tags)
             VALUES ('skill', NEW.id, NEW.name,
                     NEW.trigger_desc || ' | ' || NEW.workflow || ' | ' || COALESCE(NEW.pitfalls,''), '');
           END;""",
        """CREATE TRIGGER IF NOT EXISTS fts_skills_after_delete
           AFTER DELETE ON skills BEGIN
             DELETE FROM memory_fts WHERE item_type = 'skill' AND item_id = CAST(OLD.id AS TEXT);
           END;""",
        """CREATE TRIGGER IF NOT EXISTS fts_skills_after_update
           AFTER UPDATE ON skills BEGIN
             DELETE FROM memory_fts WHERE item_type = 'skill' AND item_id = CAST(OLD.id AS TEXT);
             INSERT INTO memory_fts(item_type, item_id, title, content, tags)
             VALUES ('skill', NEW.id, NEW.name,
                     NEW.trigger_desc || ' | ' || NEW.workflow || ' | ' || COALESCE(NEW.pitfalls,''), '');
           END;""",
        # ── conversations ────────────────────────────────────────────────
        """CREATE TRIGGER IF NOT EXISTS fts_conversations_after_insert
           AFTER INSERT ON conversations BEGIN
             INSERT INTO memory_fts(item_type, item_id, title, content, tags)
             VALUES ('conversation', NEW.id, NEW.title,
                     COALESCE(NEW.tasks_completed,'') || ' | ' || COALESCE(NEW.key_decisions,''), '');
           END;""",
        """CREATE TRIGGER IF NOT EXISTS fts_conversations_after_delete
           AFTER DELETE ON conversations BEGIN
             DELETE FROM memory_fts WHERE item_type = 'conversation' AND item_id = CAST(OLD.id AS TEXT);
           END;""",
        """CREATE TRIGGER IF NOT EXISTS fts_conversations_after_update
           AFTER UPDATE ON conversations BEGIN
             DELETE FROM memory_fts WHERE item_type = 'conversation' AND item_id = CAST(OLD.id AS TEXT);
             INSERT INTO memory_fts(item_type, item_id, title, content, tags)
             VALUES ('conversation', NEW.id, NEW.title,
                     COALESCE(NEW.tasks_completed,'') || ' | ' || COALESCE(NEW.key_decisions,''), '');
           END;""",
        # ── prompts ───────────────────────────────────────────────────────
        """CREATE TRIGGER IF NOT EXISTS fts_prompts_after_insert
           AFTER INSERT ON prompts BEGIN
             INSERT INTO memory_fts(item_type, item_id, title, content, tags)
             VALUES ('prompt', NEW.id, NEW.name,
                     NEW.role || ' | ' || NEW.description || ' | ' || COALESCE(NEW.best_for,''), '');
           END;""",
        """CREATE TRIGGER IF NOT EXISTS fts_prompts_after_delete
           AFTER DELETE ON prompts BEGIN
             DELETE FROM memory_fts WHERE item_type = 'prompt' AND item_id = CAST(OLD.id AS TEXT);
           END;""",
        """CREATE TRIGGER IF NOT EXISTS fts_prompts_after_update
           AFTER UPDATE ON prompts BEGIN
             DELETE FROM memory_fts WHERE item_type = 'prompt' AND item_id = CAST(OLD.id AS TEXT);
             INSERT INTO memory_fts(item_type, item_id, title, content, tags)
             VALUES ('prompt', NEW.id, NEW.name,
                     NEW.role || ' | ' || NEW.description || ' | ' || COALESCE(NEW.best_for,''), '');
           END;""",
        # ── sessions ──────────────────────────────────────────────────────
        """CREATE TRIGGER IF NOT EXISTS fts_sessions_after_insert
           AFTER INSERT ON sessions BEGIN
             INSERT INTO memory_fts(item_type, item_id, title, content, tags)
             VALUES ('session', NEW.id, NEW.session_id,
                     NEW.title || ' | ' || COALESCE(NEW.workflow_used,''), '');
           END;""",
        """CREATE TRIGGER IF NOT EXISTS fts_sessions_after_delete
           AFTER DELETE ON sessions BEGIN
             DELETE FROM memory_fts WHERE item_type = 'session' AND item_id = CAST(OLD.id AS TEXT);
           END;""",
        """CREATE TRIGGER IF NOT EXISTS fts_sessions_after_update
           AFTER UPDATE ON sessions BEGIN
             DELETE FROM memory_fts WHERE item_type = 'session' AND item_id = CAST(OLD.id AS TEXT);
             INSERT INTO memory_fts(item_type, item_id, title, content, tags)
             VALUES ('session', NEW.id, NEW.session_id,
                     NEW.title || ' | ' || COALESCE(NEW.workflow_used,''), '');
           END;""",
    ],
    7: [
        """CREATE TABLE IF NOT EXISTS codebase_knowledge (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            file_path TEXT NOT NULL,
            file_hash TEXT NOT NULL,
            summary TEXT NOT NULL,
            exports TEXT,
            dependencies TEXT,
            last_indexed_at TEXT DEFAULT (datetime('now')),
            UNIQUE(project_id, file_path)
        );""",
        "CREATE INDEX IF NOT EXISTS idx_codebase_knowledge_project ON codebase_knowledge(project_id);",
    ],
    # v8: embedding_status tracking, session state machine, workflow phase
    # columns, archived_memories table, and file_relationships.
    8: [
        # Per-item embedding status for visibility during model migrations
        """CREATE TABLE IF NOT EXISTS embedding_status (
            fts_rowid INTEGER PRIMARY KEY,
            item_type TEXT NOT NULL,
            item_id INTEGER NOT NULL,
            embedding_model TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now')),
            error_message TEXT
        );""",
        "CREATE INDEX IF NOT EXISTS idx_embedding_status_status ON embedding_status(status);",
        "CREATE INDEX IF NOT EXISTS idx_embedding_status_item ON embedding_status(item_type, item_id);",

        # Session state machine for workflow enforcement
        """CREATE TABLE IF NOT EXISTS session_state (
            session_id TEXT PRIMARY KEY REFERENCES sessions(session_id) ON DELETE CASCADE,
            current_phase TEXT NOT NULL DEFAULT 'analysis',
            required_roles TEXT NOT NULL DEFAULT '[]',
            completed_roles TEXT NOT NULL DEFAULT '[]',
            can_proceed INTEGER NOT NULL DEFAULT 0
        );""",

        # Workflow phase columns (extend existing workflows table)
        "ALTER TABLE workflows ADD COLUMN phases TEXT;",
        "ALTER TABLE workflows ADD COLUMN phase_requirements TEXT;",

        # Archive table for soft-deleted memories
        """CREATE TABLE IF NOT EXISTS archived_memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_type TEXT NOT NULL,
            item_id INTEGER NOT NULL,
            original_table TEXT NOT NULL,
            data TEXT NOT NULL,
            archived_at TEXT DEFAULT (datetime('now')),
            archive_reason TEXT
        );""",
        "CREATE INDEX IF NOT EXISTS idx_archived_memories_type ON archived_memories(item_type);",

        # Cross-file relationship graph for codebase knowledge
        """CREATE TABLE IF NOT EXISTS file_relationships (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            source_file TEXT NOT NULL,
            target_file TEXT NOT NULL,
            relationship_type TEXT NOT NULL,
            UNIQUE(project_id, source_file, target_file, relationship_type)
        );""",
        "CREATE INDEX IF NOT EXISTS idx_file_relationships_project ON file_relationships(project_id);",
        "CREATE INDEX IF NOT EXISTS idx_file_relationships_source ON file_relationships(project_id, source_file);",
        "CREATE INDEX IF NOT EXISTS idx_file_relationships_target ON file_relationships(project_id, target_file);",

        # File mtime for faster change detection (avoids full hash on every run)
        "ALTER TABLE codebase_knowledge ADD COLUMN file_mtime REAL;",
    ],
    # v9: Backfill embedding_status for all existing FTS entries as 'stale'
    # so the upgrade worker knows what needs re-embedding.
    9: [
        """INSERT OR IGNORE INTO embedding_status (fts_rowid, item_type, item_id, status)
           SELECT rowid, item_type, CAST(item_id AS INTEGER), 'stale'
           FROM memory_fts;""",
    ],
}


# ── Downgrade definitions ────────────────────────────────────────────
# SQLite cannot DROP columns (pre-3.35) or drop triggers easily, so
# downgrades focus on dropping tables/indexes added in each version.
# ALTER TABLE ADD COLUMN steps are marked as no-ops.

DOWNGRADES = {
    9: [
        "DELETE FROM embedding_status;",
    ],
    8: [
        "DROP TABLE IF EXISTS file_relationships;",
        "DROP TABLE IF EXISTS archived_memories;",
        "DROP TABLE IF EXISTS session_state;",
        "DROP TABLE IF EXISTS embedding_status;",
        # Note: ALTER TABLE ADD COLUMN (phases, phase_requirements, file_mtime)
        # cannot be reversed in SQLite < 3.35 — columns remain but are unused.
    ],
    7: [
        "DROP TABLE IF EXISTS codebase_knowledge;",
        "DROP INDEX IF EXISTS idx_codebase_knowledge_project;",
    ],
    # Earlier versions are rarely rolled back in practice; omitted for brevity.
}


def run_migrations(conn, current_version: int, target_version: int) -> bool:
    """Run sequential upgrade migrations from current_version+1 to target_version."""
    from .database import rebuild_fts  # lazy import to avoid circular

    for version in range(current_version + 1, target_version + 1):
        if version not in MIGRATIONS:
            continue
        print(f"→ Running database migration v{version}...")
        for query in MIGRATIONS[version]:
            conn.execute(query)

        if version == 6:
            print("→ v6: Rebuilding FTS index to resolve existing drift...")
            rebuild_fts(conn)
            print("  ✓ FTS index rebuilt successfully.")

        conn.execute(
            "UPDATE schema_meta SET value = ? WHERE key = 'version'", (str(version),)
        )
        print(f"  ✓ Migration v{version} complete.")

    return True


def downgrade_to(conn, current_version: int, target_version: int) -> bool:
    """Run sequential downgrade migrations from current_version down to target_version."""
    if target_version >= current_version:
        return True

    for version in range(current_version, target_version, -1):
        if version not in DOWNGRADES:
            print(f"  ⚠ No downgrade script for v{version} → v{version - 1}; skipping.")
            continue
        print(f"→ Downgrading v{version} → v{version - 1}...")
        for query in DOWNGRADES[version]:
            try:
                conn.execute(query)
            except Exception as e:
                print(f"  ⚠ Downgrade step skipped ({e}): {query[:80]}")
        conn.execute(
            "UPDATE schema_meta SET value = ? WHERE key = 'version'",
            (str(version - 1),),
        )
        print(f"  ✓ Downgraded to v{version - 1}.")

    return True


def backup_before_migration(db_path: str, version: int) -> str | None:
    """Copy the DB file to a timestamped backup before a destructive migration.

    Returns the backup path, or None if the source file doesn't exist yet.
    """
    if not os.path.exists(db_path):
        return None
    backup_dir = os.path.join(os.path.dirname(db_path), "backups")
    os.makedirs(backup_dir, exist_ok=True)
    backup_path = os.path.join(backup_dir, f"pre-migration-v{version}.db")
    shutil.copy2(db_path, backup_path)
    return backup_path
