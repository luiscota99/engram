"""Smoke tests for CLI command functions."""
from __future__ import annotations

import io
import sys

import pytest

from src.database import get_connection, index_in_fts, link_tags


# ── Helpers ──────────────────────────────────────────────────────────

def _seed_skill(db_path: str, name: str = "Test Skill", domain: str = "engineering") -> int:
    """Insert a single skill into the test DB and return its id."""
    with get_connection(db_path) as conn:
        cursor = conn.execute(
            "INSERT INTO skills (name, domain, trigger_desc, workflow) VALUES (?, ?, ?, ?)",
            (name, domain, "When to use it", "Step 1\nStep 2"),
        )
        sid = cursor.lastrowid
        link_tags(conn, "skill", sid, ["test"])
        index_in_fts(conn, "skill", sid, name, "When to use it | Step 1 Step 2", ["test"])
    return sid


def _capture_output(func, *args, **kwargs) -> str:
    """Capture stdout from a function call."""
    buf = io.StringIO()
    old_stdout = sys.stdout
    sys.stdout = buf
    try:
        func(*args, **kwargs)
    finally:
        sys.stdout = old_stdout
    return buf.getvalue()


@pytest.fixture(autouse=True)
def _patch_db_path(test_db, monkeypatch):
    """Point module-level DB_PATH in src.database to the test DB for this test."""
    import src.database as _db
    monkeypatch.setattr(_db, "DB_PATH", test_db["path"])


# ── cmd_stats ────────────────────────────────────────────────────────

class TestCmdStats:
    def test_runs_without_error(self, test_db):
        from src.cli.commands.memory import cmd_stats

        class Args:
            pass

        output = _capture_output(cmd_stats, Args())
        assert "Mistakes" in output
        assert "Skills" in output
        assert "Patterns" in output

    def test_shows_zero_counts_on_empty_db(self, test_db):
        from src.cli.commands.memory import cmd_stats

        class Args:
            pass

        output = _capture_output(cmd_stats, Args())
        assert "0" in output


# ── cmd_search ───────────────────────────────────────────────────────

class TestCmdSearch:
    def test_empty_db_returns_no_results(self, test_db):
        from src.cli.commands.memory import cmd_search

        class Args:
            query = ["anything"]
            type = None
            tags = None
            limit = 10

        output = _capture_output(cmd_search, Args())
        assert "No results found" in output

    def test_returns_result_for_indexed_item(self, test_db):
        from src.cli.commands.memory import cmd_search

        _seed_skill(test_db["path"], name="Docker Deploy Workflow")

        class Args:
            query = ["Docker"]
            type = None
            tags = None
            limit = 10

        output = _capture_output(cmd_search, Args())
        assert "Docker Deploy Workflow" in output


# ── cmd_recent ───────────────────────────────────────────────────────

class TestCmdRecent:
    def test_empty_db_returns_gracefully(self, test_db):
        from src.cli.commands.memory import cmd_recent

        class Args:
            n = 10
            type = None

        output = _capture_output(cmd_recent, Args())
        assert "No entries" in output

    def test_returns_seeded_skill(self, test_db):
        from src.cli.commands.memory import cmd_recent

        _seed_skill(test_db["path"], name="Recent Skill")

        class Args:
            n = 10
            type = None

        output = _capture_output(cmd_recent, Args())
        assert "Recent Skill" in output


# ── cmd_list ─────────────────────────────────────────────────────────

class TestCmdList:
    def test_list_skills_empty(self, test_db):
        from src.cli.commands.memory import cmd_list

        class Args:
            kind = "skills"

        output = _capture_output(cmd_list, Args())
        assert "Skills" in output

    def test_list_skills_with_seeded_entry(self, test_db):
        from src.cli.commands.memory import cmd_list

        _seed_skill(test_db["path"], name="Listed Skill", domain="devops")

        class Args:
            kind = "skills"

        output = _capture_output(cmd_list, Args())
        assert "Listed Skill" in output
        assert "devops" in output

    def test_list_mistakes_empty(self, test_db):
        from src.cli.commands.memory import cmd_list

        class Args:
            kind = "mistakes"

        output = _capture_output(cmd_list, Args())
        assert "Mistakes" in output

    def test_list_patterns_empty(self, test_db):
        from src.cli.commands.memory import cmd_list

        class Args:
            kind = "patterns"

        output = _capture_output(cmd_list, Args())
        assert "Patterns" in output


# ── cmd_suggest_capture ──────────────────────────────────────────────

class TestCmdSuggestCapture:
    def test_outputs_suggestion(self, test_db):
        from src.cli.commands.memory import cmd_suggest_capture

        class Args:
            task = "Fixed the broken image pipeline"
            outcome = "Resolved by adding a null check before rendering"
            errors = "TypeError: NoneType has no attribute render"
            files = "pipeline.py,renderer.py"
            json = False

        output = _capture_output(cmd_suggest_capture, Args())
        assert "Engram Memory Capture Suggestion" in output

    def test_no_errors_still_suggests_skill(self, test_db):
        from src.cli.commands.memory import cmd_suggest_capture

        class Args:
            task = "Set up the deployment workflow"
            outcome = "Successfully deployed using the new process"
            errors = None
            files = None
            json = False

        output = _capture_output(cmd_suggest_capture, Args())
        assert "Skill" in output

    def test_json_flag_emits_valid_json(self, test_db):
        import json

        from src.cli.commands.memory import cmd_suggest_capture

        class Args:
            task = "Set up the deployment workflow"
            outcome = "Successfully deployed using the new process"
            errors = None
            files = None
            json = True

        output = _capture_output(cmd_suggest_capture, Args())
        data = json.loads(output)
        assert "suggested_types" in data
        assert "confidence" in data
        assert "domain" in data
