"""
Database migrations runner for Engram.
"""

MIGRATIONS = {2: ["CREATE TABLE IF NOT EXISTS _test_migration_v2 (id INTEGER PRIMARY KEY);"]}


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
