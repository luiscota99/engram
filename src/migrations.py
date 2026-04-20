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
