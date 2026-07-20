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

import logging
import os
import shutil

logger = logging.getLogger(__name__)

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
    # v10: Pinned memories + consolidation fingerprint state
    10: [
        """CREATE TABLE IF NOT EXISTS item_pins (
            item_type TEXT NOT NULL,
            item_id INTEGER NOT NULL,
            pinned_at TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (item_type, item_id)
        );""",
        "CREATE INDEX IF NOT EXISTS idx_item_pins_type ON item_pins(item_type);",
        """CREATE TABLE IF NOT EXISTS consolidation_state (
            key TEXT PRIMARY KEY,
            fingerprint TEXT NOT NULL,
            updated_at TEXT DEFAULT (datetime('now'))
        );""",
    ],
    11: [
        "ALTER TABLE mistakes ADD COLUMN superseded_by INTEGER;",
        "ALTER TABLE patterns ADD COLUMN superseded_by INTEGER;",
        "ALTER TABLE skills ADD COLUMN superseded_by INTEGER;",
        """CREATE TABLE IF NOT EXISTS memory_facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subject TEXT NOT NULL,
            predicate TEXT NOT NULL,
            object TEXT NOT NULL,
            valid_from TEXT NOT NULL DEFAULT (date('now')),
            valid_until TEXT,
            source_type TEXT,
            source_id INTEGER
        );""",
        "CREATE INDEX IF NOT EXISTS idx_memory_facts_subject ON memory_facts(subject);",
        "INSERT OR IGNORE INTO schema_meta (key, value) VALUES ('vec_dimension', '768');",
        "INSERT OR IGNORE INTO schema_meta (key, value) VALUES ('embed_model', 'nomic-embed-text');",
    ],
    12: [
        # L2-normalize all stored vectors in place. Ollama's legacy
        # /api/embeddings returned unnormalized vectors; the newer /api/embed
        # (used by batched sweeps) returns unit vectors. Mixed norms under
        # euclidean KNN silently partition the index — see src/embeddings.py
        # l2_normalize. Pure rescaling: no re-embedding required.
        lambda conn: _normalize_vec_memory(conn),
    ],
    13: [
        """CREATE TABLE IF NOT EXISTS reflexes (
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
            last_status TEXT
        );""",
    ],
    14: [
        # Callable so a fresh-schema DB (column already present) migrates cleanly.
        lambda conn: _add_column_if_missing(conn, "reflexes", "fail_streak", "INTEGER DEFAULT 0"),
    ],
    15: [
        """CREATE TABLE IF NOT EXISTS reflex_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            reflex_id INTEGER NOT NULL REFERENCES reflexes(id) ON DELETE CASCADE,
            started_at TEXT NOT NULL,
            duration_ms INTEGER,
            status TEXT NOT NULL
        );""",
        "CREATE INDEX IF NOT EXISTS idx_reflex_runs_reflex ON reflex_runs(reflex_id);",
    ],
    16: [
        # Rebuild memory_fts with porter stemming. Rowids are copied verbatim
        # so vec_memory (keyed by the same rowids) stays aligned — no re-embed.
        lambda conn: _rebuild_fts_with_porter(conn),
    ],
    18: [
        # read_only: a safety dimension orthogonal to kind. Mutating is the SAFE
        # default (0) — a script only earns free-run treatment by explicit human
        # mark, never by inference.
        lambda conn: _add_column_if_missing(conn, "reflexes", "read_only", "INTEGER NOT NULL DEFAULT 0"),
    ],
    19: [
        """CREATE TABLE IF NOT EXISTS skill_tests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_type TEXT NOT NULL,
            item_id INTEGER NOT NULL,
            scenario TEXT NOT NULL,
            assertion TEXT NOT NULL,
            grader TEXT NOT NULL DEFAULT 'contains',
            last_result TEXT,
            baseline_passed INTEGER,
            treatment_passed INTEGER,
            last_run_at TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );""",
        "CREATE INDEX IF NOT EXISTS idx_skill_tests_item ON skill_tests(item_type, item_id);",
    ],
    20: [
        # Typed relationships between memory items — the useful, LLM-era-appropriate
        # slice of a knowledge graph (a small closed vocabulary of edge types),
        # without an OWL/RDF stack. `source` distinguishes manual links from
        # auto-derived ones (e.g. a merge recording 'supersedes').
        """CREATE TABLE IF NOT EXISTS memory_relations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_type TEXT NOT NULL,
            from_id INTEGER NOT NULL,
            to_type TEXT NOT NULL,
            to_id INTEGER NOT NULL,
            relation TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'manual',
            created_at TEXT DEFAULT (datetime('now')),
            UNIQUE(from_type, from_id, to_type, to_id, relation)
        );""",
        "CREATE INDEX IF NOT EXISTS idx_relations_from ON memory_relations(from_type, from_id);",
        "CREATE INDEX IF NOT EXISTS idx_relations_to ON memory_relations(to_type, to_id);",
    ],
    21: [
        # Crash-proof session checkpoints — one row per (project, session),
        # upserted by the Stop hook on every agent turn. Operational state,
        # NOT a memory item type: never FTS-indexed, never embedded; read
        # only via `engram resume` / memory_resume.
        """CREATE TABLE IF NOT EXISTS checkpoints (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_path TEXT NOT NULL,
            session_id TEXT NOT NULL,
            last_prompt TEXT NOT NULL DEFAULT '',
            last_summary TEXT NOT NULL DEFAULT '',
            git_head TEXT NOT NULL DEFAULT '',
            git_branch TEXT NOT NULL DEFAULT '',
            turn_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now')),
            UNIQUE(project_path, session_id)
        );""",
        "CREATE INDEX IF NOT EXISTS idx_checkpoints_project ON checkpoints(project_path, updated_at);",
    ],
    22: [
        # Retrieval feedback — the reward/discourage signal for RANKING only.
        # helpful=+1 rewards, helpful=-1 demotes; neither ever deletes or
        # archives (dormant knowledge is fine — deletion is the user's call,
        # proposed via the inbox). One events table for every item type, so
        # rank-time aggregation is a single query — no per-type fetch drift.
        """CREATE TABLE IF NOT EXISTS retrieval_feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_type TEXT NOT NULL,
            item_id INTEGER NOT NULL,
            helpful INTEGER NOT NULL CHECK (helpful IN (1, -1)),
            query TEXT,
            source TEXT NOT NULL DEFAULT 'manual',
            created_at TEXT DEFAULT (datetime('now'))
        );""",
        "CREATE INDEX IF NOT EXISTS idx_feedback_item ON retrieval_feedback(item_type, item_id);",
    ],
    23: [
        # Single-owner FTS writes: drop the v6 triggers. They and index_in_fts()
        # both indexed items, with mismatched item_id types (trigger: INTEGER,
        # code: TEXT) — every add produced a degraded duplicate row, and the
        # update trigger DELETEd the good text row and reinserted a bare
        # integer one, so routine usage bumps destroyed tags + vector links.
        # Fresh DBs never had these triggers; index_in_fts (which alone
        # maintains vec_memory + embedding_status) is the one owner. All
        # insert paths were verified to call it (2026-07-17 audit).
        lambda conn: _drop_fts_triggers(conn),
        lambda conn: _repair_fts_type_drift(conn),
    ],
    24: [
        # Race-proof inbox dedup: "unique while open" as a partial unique
        # index, so concurrent filers can't double-file a finding_key. Filing
        # uses INSERT OR IGNORE against this index.
        """CREATE UNIQUE INDEX IF NOT EXISTS idx_inbox_open_finding
           ON inbox(finding_key) WHERE status = 'open' AND finding_key IS NOT NULL;""",
    ],
    26: [
        # Milestone handoffs: the every-turn checkpoint captures whatever the
        # last message happened to be; a milestone is a DELIBERATE briefing
        # (agent- or user-written at a good stopping point, or auto-composed).
        # Kept as columns on the same row: turn fields churn every turn,
        # milestone fields persist until the next milestone.
        lambda conn: _add_column_if_missing(conn, "checkpoints", "milestone_summary", "TEXT"),
        lambda conn: _add_column_if_missing(conn, "checkpoints", "milestone_at", "TEXT"),
    ],
    25: [
        # Per-memory forgetting curves (FSRS-4.5): stability/difficulty state
        # evolved by usage (recall@good), helped feedback (recall@easy) and
        # unhelpful feedback (lapse — never increases stability). One table
        # for all item types (no per-type column sprawl); items without a row
        # keep the fixed-half-life ranking behavior unchanged.
        """CREATE TABLE IF NOT EXISTS memory_dynamics (
            item_type TEXT NOT NULL,
            item_id INTEGER NOT NULL,
            stability REAL NOT NULL,
            difficulty REAL NOT NULL DEFAULT 5.0,
            last_event_at TEXT NOT NULL DEFAULT (datetime('now')),
            reps INTEGER NOT NULL DEFAULT 0,
            lapses INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (item_type, item_id)
        );""",
    ],
    17: [
        lambda conn: _add_column_if_missing(conn, "reflexes", "kind", "TEXT NOT NULL DEFAULT 'action'"),
        """CREATE TABLE IF NOT EXISTS inbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL DEFAULT 'alert',
            severity TEXT NOT NULL DEFAULT 'warning',
            title TEXT NOT NULL,
            body TEXT,
            source TEXT,
            finding_key TEXT,
            proposed_reflex_id INTEGER REFERENCES reflexes(id) ON DELETE SET NULL,
            proposed_params TEXT,
            status TEXT NOT NULL DEFAULT 'open',
            created_at TEXT DEFAULT (datetime('now')),
            decided_at TEXT
        );""",
        "CREATE INDEX IF NOT EXISTS idx_inbox_status ON inbox(status);",
        "CREATE INDEX IF NOT EXISTS idx_inbox_finding ON inbox(finding_key);",
        """CREATE TABLE IF NOT EXISTS reflex_changes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            reflex_run_id INTEGER REFERENCES reflex_runs(id) ON DELETE CASCADE,
            target TEXT NOT NULL,
            before_value TEXT,
            after_value TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );""",
    ],
}


def _swap_fts_table(conn, tokenize_clause: str, staging_name: str) -> None:
    """Rebuild memory_fts with a new tokenizer, preserving rowids exactly
    (vec_memory is keyed by the same rowids — no re-embed needed).

    Dependent triggers reference memory_fts, and SQLite validates trigger
    bodies during ALTER TABLE RENAME — so they are saved, dropped, and
    recreated around the swap.
    """
    cols = [r[1] for r in conn.execute("PRAGMA table_info(memory_fts)").fetchall()]
    if not cols:
        return
    triggers = conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='trigger' AND sql LIKE '%memory_fts%'"
    ).fetchall()
    for t in triggers:
        conn.execute(f"DROP TRIGGER IF EXISTS {t[0]}")
    conn.execute(
        f"CREATE VIRTUAL TABLE {staging_name} USING fts5("
        f"item_type, item_id UNINDEXED, title, content, tags{tokenize_clause})"
    )
    conn.execute(
        f"INSERT INTO {staging_name}(rowid, item_type, item_id, title, content, tags) "
        f"SELECT rowid, item_type, item_id, title, content, tags FROM memory_fts"
    )
    conn.execute("DROP TABLE memory_fts")
    conn.execute(f"ALTER TABLE {staging_name} RENAME TO memory_fts")
    for t in triggers:
        if t[1]:
            conn.execute(t[1])


def _rebuild_fts_with_porter(conn) -> None:
    _swap_fts_table(conn, ", tokenize='porter unicode61'", "memory_fts_v16")


def _drop_fts_triggers(conn) -> None:
    """v23: remove every fts_* trigger — index_in_fts() is the sole FTS owner."""
    names = [
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger' AND name LIKE 'fts\\_%' ESCAPE '\\'"
        ).fetchall()
    ]
    for name in names:
        conn.execute(f'DROP TRIGGER IF EXISTS "{name}"')
    if names:
        logger.info(f"  ✓ Dropped {len(names)} legacy FTS triggers (single-owner writes).")


def _repair_fts_type_drift(conn) -> None:
    """v23: repair the damage the dual write path left behind.

    1. Integer-typed rows WITH a text sibling are pure duplicates → delete.
    2. Orphan integer-typed rows (the good row was already destroyed by the
       update trigger) → re-key as TEXT in place so lookups and cleanup
       DELETEs match again. Their vector is regenerated later (the row has no
       vec_memory/embedding_status entry, which doctor/self-check report as
       drift and `engram reembed`/`doctor --repair` fix) — a migration must
       not call the embedding endpoint.
    """
    dups = conn.execute(
        """SELECT f.rowid FROM memory_fts f
           WHERE typeof(f.item_id) = 'integer'
             AND EXISTS (SELECT 1 FROM memory_fts g
                         WHERE g.item_type = f.item_type
                           AND typeof(g.item_id) = 'text'
                           AND g.item_id = CAST(f.item_id AS TEXT))"""
    ).fetchall()
    for (rowid,) in dups:
        conn.execute("DELETE FROM memory_fts WHERE rowid = ?", (rowid,))

    orphans = conn.execute(
        "SELECT rowid, item_type, item_id, title, content, tags FROM memory_fts "
        "WHERE typeof(item_id) = 'integer'"
    ).fetchall()
    for rowid, item_type, item_id, title, content, tags in orphans:
        conn.execute("DELETE FROM memory_fts WHERE rowid = ?", (rowid,))
        conn.execute(
            "INSERT INTO memory_fts (item_type, item_id, title, content, tags) "
            "VALUES (?, ?, ?, ?, ?)",
            (item_type, str(item_id), title or "", content or "", tags or ""),
        )
    if dups or orphans:
        logger.info(
            f"  ✓ FTS repair: removed {len(dups)} duplicate rows, "
            f"re-keyed {len(orphans)} orphan integer rows."
        )


def _add_column_if_missing(conn, table: str, column: str, decl: str) -> None:
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def _normalize_vec_memory(conn) -> None:
    import json as _json
    import math as _math
    import struct as _struct

    try:
        rows = conn.execute("SELECT rowid, embedding FROM vec_memory").fetchall()
    except Exception:
        return  # vec extension unavailable; nothing to normalize
    fixed = 0
    for row in rows:
        raw = row["embedding"]
        if isinstance(raw, (bytes, bytearray)):
            vec = list(_struct.unpack(f"{len(raw) // 4}f", raw))
        else:
            vec = _json.loads(raw)
        norm = _math.sqrt(sum(x * x for x in vec))
        if norm <= 0 or abs(norm - 1.0) < 1e-3:
            continue  # empty or already unit length
        normed = [x / norm for x in vec]
        # vec0 virtual tables don't support UPDATE reliably (<0.1.10): delete+insert
        conn.execute("DELETE FROM vec_memory WHERE rowid = ?", (row["rowid"],))
        conn.execute(
            "INSERT INTO vec_memory(rowid, embedding) VALUES (?, ?)",
            (row["rowid"], _json.dumps(normed)),
        )
        fixed += 1
    if fixed:
        logger.info(f"  ✓ Normalized {fixed} stored vectors to unit length.")


# ── Downgrade definitions ────────────────────────────────────────────
# SQLite cannot DROP columns (pre-3.35) or drop triggers easily, so
# downgrades focus on dropping tables/indexes added in each version.
# ALTER TABLE ADD COLUMN steps are marked as no-ops.

DOWNGRADES = {
    26: [
        # ALTER ADD COLUMN is left in place on downgrade (harmless).
    ],
    25: [
        "DROP TABLE IF EXISTS memory_dynamics;",
    ],
    24: [
        "DROP INDEX IF EXISTS idx_inbox_open_finding;",
    ],
    23: [
        # No-op by design: the dropped triggers were a corruption source; a
        # downgrade must not resurrect them, and the dedup repair is strictly
        # corrective. Older code works fine against trigger-less FTS.
    ],
    22: [
        "DROP TABLE IF EXISTS retrieval_feedback;",
    ],
    21: [
        "DROP TABLE IF EXISTS checkpoints;",
    ],
    20: [
        "DROP TABLE IF EXISTS memory_relations;",
    ],
    19: [
        "DROP TABLE IF EXISTS skill_tests;",
    ],
    18: [
        # ALTER ADD COLUMN is left in place on downgrade (harmless).
    ],
    17: [
        "DROP TABLE IF EXISTS inbox;",
        "DROP TABLE IF EXISTS reflex_changes;",
    ],
    16: [
        # Reverse rebuild without porter (stable rowids, same shape).
        lambda conn: _swap_fts_table(conn, "", "memory_fts_v15"),
    ],
    15: [
        "DROP TABLE IF EXISTS reflex_runs;",
    ],
    14: [
        # ALTER TABLE ADD COLUMN is left in place on downgrade (harmless).
    ],
    13: [
        "DROP TABLE IF EXISTS reflexes;",
    ],
    12: [
        # Normalization is not reversible (original norms were discarded), and
        # doesn't need to be: unit vectors rank identically for cosine use.
    ],
    11: [
        "DROP TABLE IF EXISTS memory_facts;",
    ],
    10: [
        "DROP TABLE IF EXISTS consolidation_state;",
        "DROP TABLE IF EXISTS item_pins;",
    ],
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
        logger.info(f"→ Running database migration v{version}...")
        for step in MIGRATIONS[version]:
            if callable(step):
                step(conn)
            else:
                conn.execute(step)

        if version == 6:
            logger.info("→ v6: Rebuilding FTS index to resolve existing drift...")
            rebuild_fts(conn)
            logger.info("  ✓ FTS index rebuilt successfully.")

        conn.execute(
            "UPDATE schema_meta SET value = ? WHERE key = 'version'", (str(version),)
        )
        logger.info(f"  ✓ Migration v{version} complete.")

    return True


def downgrade_to(conn, current_version: int, target_version: int) -> bool:
    """Run sequential downgrade migrations from current_version down to target_version."""
    if target_version >= current_version:
        return True

    for version in range(current_version, target_version, -1):
        if version not in DOWNGRADES:
            logger.info(f"  ⚠ No downgrade script for v{version} → v{version - 1}; skipping.")
            continue
        logger.info(f"→ Downgrading v{version} → v{version - 1}...")
        for query in DOWNGRADES[version]:
            try:
                if callable(query):
                    query(conn)
                else:
                    conn.execute(query)
            except Exception as e:
                logger.info(f"  ⚠ Downgrade step skipped ({e}): {str(query)[:80]}")
        conn.execute(
            "UPDATE schema_meta SET value = ? WHERE key = 'version'",
            (str(version - 1),),
        )
        logger.info(f"  ✓ Downgraded to v{version - 1}.")

    return True


def backup_before_migration(db_path: str, version: int) -> str | None:
    """Snapshot the DB to a backup file before a destructive migration.

    Uses the SQLite backup API: under WAL, recently committed transactions
    live in ``-wal``, so a bare file copy yields a valid-but-stale snapshot
    that silently misses them. Falls back to a file copy only if the backup
    API fails (better a stale backup than none). Returns the backup path, or
    None if the source doesn't exist yet.
    """
    if not os.path.exists(db_path):
        return None
    backup_dir = os.path.join(os.path.dirname(db_path), "backups")
    os.makedirs(backup_dir, exist_ok=True)
    backup_path = os.path.join(backup_dir, f"pre-migration-v{version}.db")
    try:
        try:
            import sqlean as sqlite3  # matches the connection layer's driver
        except ImportError:
            import sqlite3
        # sqlean-py may lack complete stubs; runtime matches stdlib sqlite3 API.
        src = sqlite3.connect(db_path)  # type: ignore[attr-defined]
        try:
            dst = sqlite3.connect(backup_path)  # type: ignore[attr-defined]
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
    except Exception:
        logger.warning(
            "SQLite backup API failed; falling back to file copy (may miss WAL)",
            exc_info=True,
        )
        shutil.copy2(db_path, backup_path)
    return backup_path
