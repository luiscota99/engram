"""
Database backup and export tools for Engram.
"""

from __future__ import annotations

import datetime
import json
import os
import subprocess

from .database import get_connection, get_db_path


def export_to_json(conn):
    """Export the core database tables to a dictionary.

    Includes reflexes and their run history: reflex scripts exist only in the
    database (they are data, not repo code), so a backup that skips them loses
    every approved automation.
    """
    data = {}
    tables = [
        "mistakes",
        "patterns",
        "skills",
        "conversations",
        "prompts",
        "reflexes",
        "reflex_runs",
        # July 2026 audit: the export used to silently omit these — a backup
        # that drops tags, pins, relations, or the inbox is not a backup.
        "sessions",
        "session_transcripts",
        "tags",
        "item_tags",
        "item_projects",
        "item_pins",
        "memory_relations",
        "memory_facts",
        "skill_tests",
        "inbox",
        "checkpoints",
        "retrieval_feedback",
        "memory_dynamics",
    ]
    for table in tables:
        try:
            rows = conn.execute(f"SELECT * FROM {table}").fetchall()
        except Exception:
            continue  # table absent in pre-migration databases
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


def restore_database(backup_path: str, db_path: str | None = None) -> dict:
    """Restore the memory DB from a SQLite backup file — validated, snapshotted.

    The July 2026 audit's finding: backups existed, restore didn't — untested
    by construction. This is the other half. Safety order:

    1. Validate the backup: it must open, pass integrity_check, and contain a
       schema_meta version (any version — migrations run forward on next use).
    2. Snapshot the CURRENT db (``pre-restore-<ts>.db``, backup API, WAL-safe)
       so a restore is itself reversible.
    3. Replace atomically via the SQLite backup API (never a file copy).

    Returns {restored_from, pre_restore_snapshot, backup_schema_version}.
    Raises ValueError on an invalid backup — the current DB is untouched.
    """
    try:
        import sqlean as sqlite3  # matches the connection layer's driver
    except ImportError:
        import sqlite3

    db_path = db_path or get_db_path()
    if not os.path.exists(backup_path):
        raise ValueError(f"Backup file not found: {backup_path}")

    # 1. Validate
    src = sqlite3.connect(backup_path)  # type: ignore[attr-defined]
    try:
        ok = src.execute("PRAGMA integrity_check").fetchone()[0]
        if ok != "ok":
            raise ValueError(f"Backup fails integrity_check: {ok}")
        row = src.execute(
            "SELECT value FROM schema_meta WHERE key = 'version'"
        ).fetchone()
        if not row:
            raise ValueError("Backup has no schema_meta version — not an Engram DB")
        backup_version = int(row[0])
    except sqlite3.Error as e:  # type: ignore[attr-defined]
        raise ValueError(f"Not a readable SQLite database: {e}") from e
    finally:
        src.close()

    # 2. Snapshot current (only if it exists)
    snapshot = None
    if os.path.exists(db_path):
        ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        snap_dir = os.path.join(os.path.dirname(db_path), "backups")
        os.makedirs(snap_dir, exist_ok=True)
        snapshot = os.path.join(snap_dir, f"pre-restore-{ts}.db")
        cur = sqlite3.connect(db_path)  # type: ignore[attr-defined]
        try:
            dst = sqlite3.connect(snapshot)  # type: ignore[attr-defined]
            try:
                cur.backup(dst)
            finally:
                dst.close()
        finally:
            cur.close()

    # 3. Restore via backup API into the live path
    src = sqlite3.connect(backup_path)  # type: ignore[attr-defined]
    try:
        dst = sqlite3.connect(db_path)  # type: ignore[attr-defined]
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()

    return {
        "restored_from": backup_path,
        "pre_restore_snapshot": snapshot,
        "backup_schema_version": backup_version,
    }
