"""Coverage tests for src/cli/commands/maintenance.py.

Each CLI command function is driven with a fake args namespace; stdout is
captured and asserted against the real formatted output. External I/O
(Ollama embedding, subprocess git) is either avoided (empty-DB paths) or
mocked at the maintenance-command module boundary.
"""
from __future__ import annotations

import io
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from src.database import get_connection

# ── Helpers ──────────────────────────────────────────────────────────

def _capture(func, *args, **kwargs) -> str:
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        func(*args, **kwargs)
    finally:
        sys.stdout = old
    return buf.getvalue()


def _seed_gc_candidate(db_path: str, item_type: str = "mistake") -> int:
    """Insert a never-used, old item so find_gc_candidates picks it up."""
    old = "2000-01-01T00:00:00"
    with get_connection(db_path) as conn:
        if item_type == "mistake":
            cur = conn.execute(
                "INSERT INTO mistakes (date, context, mistake, root_cause, fix, prevention, "
                "usage_count, created_at) VALUES (?,?,?,?,?,?,0,?)",
                (old, "ctx", "did a bad thing", "cause", "fix", "prevent", old),
            )
        else:
            cur = conn.execute(
                "INSERT INTO skills (name, domain, trigger_desc, workflow, usage_count, "
                "created_at) VALUES (?,?,?,?,0,?)",
                ("Old Skill", "eng", "trigger", "wf", old),
            )
        return cur.lastrowid


def _seed_embedding_status(db_path: str, rows: list[tuple]) -> None:
    """rows: list of (fts_rowid, status)."""
    with get_connection(db_path) as conn:
        for i, (rowid, status) in enumerate(rows):
            conn.execute(
                "INSERT INTO embedding_status (fts_rowid, item_type, item_id, status) "
                "VALUES (?,?,?,?)",
                (rowid, "mistake", i + 1, status),
            )


# ── cmd_gc ───────────────────────────────────────────────────────────

class TestCmdGc:
    def test_no_candidates_on_empty_db(self, test_db):
        from src.cli.commands.maintenance import cmd_gc

        out = _capture(cmd_gc, SimpleNamespace(mode="dry-run", days=180))
        assert "No GC candidates found." in out
        assert "threshold: never used" in out

    def test_dry_run_lists_candidate(self, test_db):
        from src.cli.commands.maintenance import cmd_gc

        _seed_gc_candidate(test_db["path"], "mistake")
        out = _capture(cmd_gc, SimpleNamespace(mode="dry-run", days=180))
        assert "GC Candidates (1)" in out
        assert "[MISTAKE]" in out
        assert "2000-01-01T00:00:00" in out
        assert "Dry-run complete" in out

    def test_archive_mode_processes_and_removes_item(self, test_db):
        from src.cli.commands.maintenance import cmd_gc

        mid = _seed_gc_candidate(test_db["path"], "mistake")
        out = _capture(cmd_gc, SimpleNamespace(mode="archive", days=180))
        assert "Archived 1 of 1 items." in out
        # side effect: the live row is gone and an archive row exists
        with get_connection(test_db["path"]) as conn:
            assert conn.execute(
                "SELECT COUNT(*) c FROM mistakes WHERE id=?", (mid,)
            ).fetchone()["c"] == 0
            assert conn.execute(
                "SELECT COUNT(*) c FROM archived_memories WHERE item_type='mistake'"
            ).fetchone()["c"] == 1

    def test_blocked_path_prints_reason(self, test_db):
        from src.cli.commands.maintenance import cmd_gc

        blocked = {"blocked": True, "reason": "GC blocked: would affect 9/10 items (90%)"}
        with patch("src.cli.commands.maintenance.run_gc", return_value=blocked):
            out = _capture(cmd_gc, SimpleNamespace(mode="delete", days=180))
        assert "GC blocked by safety guardrail" in out
        assert "would affect 9/10 items (90%)" in out


# ── cmd_doctor ───────────────────────────────────────────────────────

class TestCmdDoctor:
    def test_reports_clean_empty_db(self, test_db):
        from src.cli.commands.maintenance import cmd_doctor

        out = _capture(cmd_doctor, SimpleNamespace(repair=False))
        assert "Engram Diagnostics" in out
        assert "No orphaned tags found" in out

    def test_repair_deletes_orphaned_tag(self, test_db):
        from src.cli.commands.maintenance import cmd_doctor

        with get_connection(test_db["path"]) as conn:
            conn.execute("INSERT INTO tags (name) VALUES ('orphan')")
        out = _capture(cmd_doctor, SimpleNamespace(repair=True))
        assert "orphaned tags" in out
        assert "Deleted orphaned tags" in out
        with get_connection(test_db["path"]) as conn:
            assert conn.execute(
                "SELECT COUNT(*) c FROM tags WHERE name='orphan'"
            ).fetchone()["c"] == 0


# ── cmd_backup ───────────────────────────────────────────────────────

class TestCmdBackup:
    def test_writes_json_backup_file(self, test_db):
        import json
        import os

        from src.cli.commands.maintenance import cmd_backup

        out = _capture(cmd_backup, SimpleNamespace(git=False))
        assert "Database exported successfully to:" in out
        backup_dir = os.path.join(os.path.dirname(test_db["path"]), "backups")
        files = os.listdir(backup_dir)
        assert files and all(f.startswith("memory_backup_") for f in files)
        data = json.loads(open(os.path.join(backup_dir, files[0])).read())
        assert "skills" in data and "mistakes" in data


# ── cmd_efficiency ───────────────────────────────────────────────────

class TestCmdEfficiency:
    def test_empty_db_reports_no_promotion(self, test_db):
        from src.cli.commands.maintenance import cmd_efficiency

        out = _capture(cmd_efficiency, SimpleNamespace())
        assert "Action Ladder — efficiency report" in out
        assert "Reflex rung" in out
        assert "No promotion candidates yet" in out

    def test_rich_report_formats_all_sections(self, test_db):
        from src.cli.commands.maintenance import cmd_efficiency

        report = {
            "reflexes_approved": 2,
            "reflexes_total": 3,
            "reflex_runs": 10,
            "auto_demotions": 1,
            "reflex_success": {"1": {"runs": 8, "ok": 6}},
            "tokens_avoided_floor": 1234,
            "reuse": {
                "skill": {"eligible": 4, "reused": 2, "rate": 0.5},
                "mistake": {"eligible": 0, "reused": 0, "rate": None},
            },
            "promotion_candidates": [{"id": 7, "name": "Deploy", "usage_count": 9}],
        }
        with patch("src.maintenance.get_efficiency_report", return_value=report):
            out = _capture(cmd_efficiency, SimpleNamespace())
        assert "Approved reflexes:   2 (of 3 drafted)" in out
        assert "Success rate:        6/8 (75%)" in out
        assert "Tokens avoided:      >= 1,234" in out
        assert "2/4 reused (50%)" in out
        # eligible=0 mistake row must be skipped
        assert "mistake" not in out.split("Recall rung")[1].split("Ready to move")[0]
        assert "engram promote 7" in out
        assert "'Deploy' used 9x" in out


# ── cmd_health ───────────────────────────────────────────────────────

class TestCmdHealth:
    def test_empty_db_no_issues(self, test_db):
        from src.cli.commands.maintenance import cmd_health

        out = _capture(cmd_health, SimpleNamespace())
        assert "Engram Health Report" in out
        assert "No embeddings tracked yet." in out
        assert "No vector drift" in out
        assert "No issues detected." in out

    def test_reports_embedding_breakdown_and_recommendations(self, test_db):
        from src.cli.commands.maintenance import cmd_health
        from src.database import index_in_fts

        _seed_embedding_status(
            test_db["path"],
            [(1, "ready"), (2, "ready"), (3, "stale"), (4, "pending"), (5, "failed")],
        )
        # A reused, 30+-day-old mistake exercises the item reuse-rate line and
        # the capture->reuse block; an FTS row with no vector drives vec_drift>0.
        with get_connection(test_db["path"]) as conn:
            conn.execute(
                "INSERT INTO mistakes (date, context, mistake, root_cause, fix, prevention, "
                "usage_count, created_at) VALUES (?,?,?,?,?,?,1,?)",
                ("2000-01-01", "c", "m", "rc", "f", "p", "2000-01-01T00:00:00"),
            )
            index_in_fts(conn, "skill", 1, "Ghost Skill", "no vector here", [])
            # index_in_fts backfills a vec_memory placeholder; drop it so the
            # FTS row is left without a vector (vec_drift > 0).
            conn.execute("DELETE FROM vec_memory")
        out = _capture(cmd_health, SimpleNamespace())
        assert "Ready:" in out
        assert "Stale:" in out and "regeneration needed" in out
        assert "Pending:" in out
        assert "Failed:" in out
        # item reuse-rate + capture->reuse section (100% of 1 eligible reused)
        assert "reuse:100%" in out
        assert "Capture → Reuse:" in out
        assert "1/1 memories captured 30+ days ago" in out
        # FTS row with no matching vector -> drift recommendation
        assert "Vector drift:" in out
        assert "FTS entries are missing vector embeddings" in out
        # stale + pending drive concrete recommendations
        assert "Run `engram reembed` to regenerate 1 stale embeddings" in out
        assert "1 items have no embeddings" in out
        assert "Recommendations:" in out


# ── cmd_merge_projects ───────────────────────────────────────────────

class TestCmdMergeProjects:
    def _two_projects(self, db_path):
        with get_connection(db_path) as conn:
            a = conn.execute(
                "INSERT INTO projects (name, path) VALUES ('src-proj', '/tmp/src')"
            ).lastrowid
            b = conn.execute(
                "INSERT INTO projects (name, path) VALUES ('dst-proj', '/tmp/dst')"
            ).lastrowid
        return a, b

    def test_dry_run_reports_without_deleting(self, test_db):
        from src.cli.commands.maintenance import cmd_merge_projects

        a, b = self._two_projects(test_db["path"])
        out = _capture(
            cmd_merge_projects,
            SimpleNamespace(merge_from=str(a), merge_into=str(b), execute=False),
        )
        assert "Merge projects (Engram DB)" in out
        assert "'src-proj'" in out and "'dst-proj'" in out
        assert "Dry-run only. Re-run with --execute to apply." in out
        with get_connection(test_db["path"]) as conn:
            assert conn.execute(
                "SELECT COUNT(*) c FROM projects WHERE id=?", (a,)
            ).fetchone()["c"] == 1

    def test_execute_removes_source_project(self, test_db):
        from src.cli.commands.maintenance import cmd_merge_projects

        a, b = self._two_projects(test_db["path"])
        out = _capture(
            cmd_merge_projects,
            SimpleNamespace(merge_from="src-proj", merge_into="dst-proj", execute=True),
        )
        assert "Source project removed" in out
        with get_connection(test_db["path"]) as conn:
            assert conn.execute(
                "SELECT COUNT(*) c FROM projects WHERE id=?", (a,)
            ).fetchone()["c"] == 0

    def test_unknown_source_raises_systemexit(self, test_db):
        from src.cli.commands.maintenance import cmd_merge_projects

        self._two_projects(test_db["path"])
        with pytest.raises(SystemExit) as exc:
            cmd_merge_projects(
                SimpleNamespace(merge_from="nope-nope", merge_into="dst-proj", execute=False)
            )
        assert "No project with path or name" in str(exc.value)

    def test_missing_spec_raises_systemexit(self, test_db):
        from src.cli.commands.maintenance import cmd_merge_projects

        self._two_projects(test_db["path"])
        with pytest.raises(SystemExit) as exc:
            cmd_merge_projects(
                SimpleNamespace(merge_from="", merge_into="dst-proj", execute=False)
            )
        assert "required" in str(exc.value)

    def test_unknown_numeric_id_raises_systemexit(self, test_db):
        from src.cli.commands.maintenance import cmd_merge_projects

        a, _ = self._two_projects(test_db["path"])
        with pytest.raises(SystemExit) as exc:
            cmd_merge_projects(
                SimpleNamespace(merge_from="99999", merge_into=str(a), execute=False)
            )
        assert "No project with id 99999" in str(exc.value)


# ── cmd_reembed ──────────────────────────────────────────────────────

class TestCmdReembed:
    def test_up_to_date_short_circuits(self, test_db):
        from src.cli.commands.maintenance import cmd_reembed

        out = _capture(cmd_reembed, SimpleNamespace(batch_size=None))
        assert "All embeddings are up to date." in out

    def test_loop_reports_progress_until_done(self, test_db):
        from src.cli.commands.maintenance import cmd_reembed

        batches = [
            {"succeeded": 2, "failed": 1, "remaining": 1, "processed": 3},
            {"succeeded": 1, "failed": 0, "remaining": 0, "processed": 1},
        ]
        with patch(
            "src.cli.commands.maintenance.get_embedding_stats",
            return_value={"stale": 2, "pending": 1},
        ), patch(
            "src.cli.commands.maintenance.reembed_stale",
            side_effect=batches,
        ) as m:
            out = _capture(cmd_reembed, SimpleNamespace(batch_size=25))
        assert "Re-embedding 3 items (stale: 2, pending: 1)" in out
        assert "1 failed this batch" in out
        assert "2 done, 1 remaining" in out
        assert "3 done, 0 remaining" in out
        assert "Re-embedding complete. 3 items updated." in out
        # batch_size arg is threaded through
        assert m.call_args_list[0].kwargs["batch_size"] == 25

    def test_loop_breaks_when_no_progress(self, test_db):
        from src.cli.commands.maintenance import cmd_reembed

        stuck = {"succeeded": 0, "failed": 0, "remaining": 5, "processed": 0}
        with patch(
            "src.cli.commands.maintenance.get_embedding_stats",
            return_value={"stale": 5, "pending": 0},
        ), patch(
            "src.cli.commands.maintenance.reembed_stale",
            return_value=stuck,
        ):
            out = _capture(cmd_reembed, SimpleNamespace(batch_size=None))
        assert "0 items updated" in out


# ── cmd_migrate ──────────────────────────────────────────────────────

class TestCmdMigrate:
    def test_default_prints_usage(self, test_db):
        from src.cli.commands.maintenance import cmd_migrate

        out = _capture(cmd_migrate, SimpleNamespace(rollback=False, mark_stale=False))
        assert "Use --rollback to restore from backup" in out

    def test_mark_stale_updates_ready_rows(self, test_db):
        from src.cli.commands.maintenance import cmd_migrate

        _seed_embedding_status(test_db["path"], [(1, "ready"), (2, "ready")])
        out = _capture(cmd_migrate, SimpleNamespace(rollback=False, mark_stale=True))
        assert "Marked 2 embeddings as stale" in out
        assert "engram reembed" in out
        with get_connection(test_db["path"]) as conn:
            assert conn.execute(
                "SELECT COUNT(*) c FROM embedding_status WHERE status='stale'"
            ).fetchone()["c"] == 2

    def test_rollback_no_backup_dir(self, test_db):
        from src.cli.commands.maintenance import cmd_migrate

        out = _capture(cmd_migrate, SimpleNamespace(rollback=True, mark_stale=False))
        assert "No backups found." in out

    def test_rollback_dir_without_migration_files(self, test_db):
        import os

        from src.cli.commands.maintenance import cmd_migrate

        os.makedirs(os.path.join(os.path.dirname(test_db["path"]), "backups"))
        out = _capture(cmd_migrate, SimpleNamespace(rollback=True, mark_stale=False))
        assert "No migration backups found." in out

    def test_rollback_restores_latest_backup(self, test_db):
        import os

        from src.cli.commands.maintenance import cmd_migrate

        backup_dir = os.path.join(os.path.dirname(test_db["path"]), "backups")
        os.makedirs(backup_dir)
        latest = os.path.join(backup_dir, "pre-migration-20260101.db")
        with open(latest, "wb") as f:
            f.write(b"SQLite format 3\x00restored-marker")
        out = _capture(cmd_migrate, SimpleNamespace(rollback=True, mark_stale=False))
        assert "Rolled back to" in out
        assert "pre-migration-20260101.db" in out
        with open(test_db["path"], "rb") as f:
            assert b"restored-marker" in f.read()


# ── cmd_migrate_embeddings ───────────────────────────────────────────

class TestCmdMigrateEmbeddings:
    def test_success_reports_counts(self, test_db):
        from src.cli.commands.maintenance import cmd_migrate_embeddings

        result = {
            "ok": True,
            "target_model": "nomic-embed-text",
            "marked_stale": 4,
            "reembed": {"succeeded": 3, "failed": 1},
        }
        with patch(
            "src.cli.commands.maintenance.migrate_embeddings_to_model",
            return_value=result,
        ):
            out = _capture(
                cmd_migrate_embeddings, SimpleNamespace(target_model="nomic-embed-text")
            )
        assert "Migrated embeddings to nomic-embed-text" in out
        assert "Marked stale: 4" in out
        assert "Re-embedded: 3 succeeded, 1 failed" in out

    def test_failure_exits_nonzero(self, test_db):
        from src.cli.commands.maintenance import cmd_migrate_embeddings

        with patch(
            "src.cli.commands.maintenance.migrate_embeddings_to_model",
            return_value={"ok": False, "error": "ollama unreachable"},
        ):
            with pytest.raises(SystemExit) as exc:
                _capture(
                    cmd_migrate_embeddings, SimpleNamespace(target_model="bad-model")
                )
        assert exc.value.code == 1


# ── cmd_sleep ────────────────────────────────────────────────────────

class TestCmdSleep:
    def test_dry_run_reports_summary(self, test_db):
        from src.cli.commands.maintenance import cmd_sleep

        out = _capture(
            cmd_sleep,
            SimpleNamespace(threshold=0.85, days=30, dry_run=True, quiet=False),
        )
        assert "Engram Sleep — consolidation report" in out
        assert "Clusters found:     0" in out
        assert "Items invalidated:  0" in out
        assert "Items archived:     0" in out
        assert "GC candidates:" in out and "(dry-run)" in out

    def test_quiet_suppresses_all_output(self, test_db):
        from src.cli.commands.maintenance import cmd_sleep

        out = _capture(
            cmd_sleep,
            SimpleNamespace(threshold=0.85, days=30, dry_run=True, quiet=True),
        )
        assert out == ""
