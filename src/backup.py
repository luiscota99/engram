"""
Database backup and export tools for Engram.
"""

import datetime
import json
import os
import subprocess

from .database import get_connection, get_db_path


def export_to_json(conn):
    """Export the core database tables to a dictionary."""
    data = {}
    tables = ["mistakes", "patterns", "skills", "conversations", "prompts"]
    for table in tables:
        rows = conn.execute(f"SELECT * FROM {table}").fetchall()
        data[table] = [dict(r) for r in rows]
    return data


def run_backup(git_sync=False):
    """Export database to JSON and optionally commit to Git."""
    base_dir = os.path.dirname(get_db_path())
    backup_dir = os.path.join(base_dir, "backups")
    os.makedirs(backup_dir, exist_ok=True)

    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    filepath = os.path.join(backup_dir, f"memory_backup_{timestamp}.json")

    with get_connection() as conn:
        data = export_to_json(conn)

    with open(filepath, "w") as f:
        json.dump(data, f, indent=2)

    print(f"✓ Database exported successfully to: {filepath}")

    if git_sync:
        print("→ Attempting Git sync...")
        try:
            # Check if directory is a git repo
            subprocess.run(["git", "status"], cwd=base_dir, check=True, capture_output=True)
            subprocess.run(["git", "add", backup_dir], cwd=base_dir, check=True)
            subprocess.run(
                ["git", "commit", "-m", f"chore: automated memory backup {timestamp}"],
                cwd=base_dir,
                check=True,
            )
            print("✓ Backup committed to Git.")
            try:
                subprocess.run(["git", "push"], cwd=base_dir, check=True, capture_output=True)
                print("✓ Backup pushed to remote repository.")
            except subprocess.CalledProcessError:
                print(
                    "  (Note: No remote configured or push failed, but commit was saved locally.)"
                )
        except subprocess.CalledProcessError:
            print("  (Note: ~/.engram is not a git repository. Skipping Git sync.)")

    return filepath
