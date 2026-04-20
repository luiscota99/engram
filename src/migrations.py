"""
Database migrations runner for Engram.
"""

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
    # After this migration, memory_fts is guaranteed to stay in sync at the DB level.
    # The runner also calls rebuild_fts() to fix any existing drift before triggers take over.
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
}




def run_migrations(conn, current_version, target_version):
    """Run sequential migrations from current to target version."""
    from .database import rebuild_fts  # lazy import to avoid circular
    for version in range(current_version + 1, target_version + 1):
        if version in MIGRATIONS:
            print(f"→ Running database migration v{version}...")
            queries = MIGRATIONS[version]
            for query in queries:
                conn.execute(query)
            # v6 special step: rebuild FTS index to fix existing drift before triggers take over
            if version == 6:
                print("→ v6: Rebuilding FTS index to resolve existing drift...")
                rebuild_fts(conn)
                print("  ✓ FTS index rebuilt successfully.")
            conn.execute("UPDATE schema_meta SET value = ? WHERE key = 'version'", (str(version),))
    return True
