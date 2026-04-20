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
}


def run_migrations(conn, current_version, target_version):
    """Run sequential migrations from current to target version."""
    for version in range(current_version + 1, target_version + 1):
        if version in MIGRATIONS:
            print(f"→ Running database migration v{version}...")
            queries = MIGRATIONS[version]
            for query in queries:
                conn.execute(query)
            conn.execute("UPDATE schema_meta SET value = ? WHERE key = 'version'", (str(version),))
    return True
