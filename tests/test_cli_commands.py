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
            project = None
            no_project = True  # test DB: avoid get_or_create_project on real cwd

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
            project = None
            no_project = True

        output = _capture_output(cmd_search, Args())
        assert "Docker Deploy Workflow" in output


# ── import-session-summary ───────────────────────────────────────────

class TestImportSessionSummary:
    def test_imports_file_and_skips_duplicate(self, test_db, tmp_path):
        from src.cli.commands.session import cmd_import_session_summary

        f = tmp_path / "session_summary.md"
        f.write_text(
            "---\ntitle: Lab Session\ndomain: engineering\ntags: rust,gba\n---\n\nDid migration work.",
            encoding="utf-8",
        )

        class Args:
            file = str(f)
            project = str(tmp_path)
            force = False

        out1 = _capture_output(cmd_import_session_summary, Args())
        assert "Imported" in out1 or "✓" in out1

        out2 = _capture_output(cmd_import_session_summary, Args())
        assert "Skip" in out2


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
        assert "Engram influence" in output

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
        assert "influence_prompt" in data
        assert "Engram influence" in data["influence_prompt"]


# ── cmd_session_help ─────────────────────────────────────────────────

class TestCmdSessionHelp:
    def test_appends_jsonl_line(self, tmp_path, monkeypatch):
        from src.cli.commands.memory import cmd_session_help

        log = tmp_path / "help.jsonl"
        monkeypatch.setenv("ENGRAM_SESSION_HELP_LOG", str(log))

        class Args:
            score = 2
            note = "Applied checklist skill"
            task = "deploy"

        output = _capture_output(cmd_session_help, Args())
        assert "Logged Session Help Score 2" in output
        assert log.read_text().strip()
        import json

        line = json.loads(log.read_text().strip().splitlines()[-1])
        assert line["score"] == 2
        assert "checklist" in line["note"]

    def test_rejects_invalid_score(self, test_db, tmp_path, monkeypatch):
        from src.cli.commands.memory import cmd_session_help

        monkeypatch.setenv("ENGRAM_SESSION_HELP_LOG", str(tmp_path / "h.jsonl"))

        class Args:
            score = 5
            note = None
            task = None

        with pytest.raises(SystemExit):
            cmd_session_help(Args())


# ── cmd_suggest_consolidate ──────────────────────────────────────────

class TestCmdSuggestConsolidate:
    """Regression: find_consolidation_candidates returns (clusters, skip_reason);
    treating the tuple as the cluster list crashed on any real candidates."""

    def _args(self):
        class Args:
            threshold = 0.8
            type = None
            limit = 10

        return Args()

    def test_empty_db_prints_no_candidates(self, test_db):
        from unittest.mock import patch

        from src.cli.commands.memory import cmd_suggest_consolidate

        with patch(
            "src.cli.commands.memory.find_consolidation_candidates",
            return_value=([], None),
        ):
            output = _capture_output(cmd_suggest_consolidate, self._args())
        assert "No consolidation candidates" in output

    def test_unchanged_fingerprint_message(self, test_db):
        from unittest.mock import patch

        from src.cli.commands.memory import cmd_suggest_consolidate

        with patch(
            "src.cli.commands.memory.find_consolidation_candidates",
            return_value=([], "unchanged"),
        ):
            output = _capture_output(cmd_suggest_consolidate, self._args())
        assert "unchanged" in output

    def test_prints_cluster_details(self, test_db):
        from unittest.mock import patch

        from src.cli.commands.memory import cmd_suggest_consolidate

        cluster = {
            "item_type": "skill",
            "cluster_size": 2,
            "avg_similarity": 0.91,
            "items": [
                {"item_id": 1, "title": "Skill A"},
                {"item_id": 2, "title": "Skill B"},
            ],
        }
        with patch(
            "src.cli.commands.memory.find_consolidation_candidates",
            return_value=([cluster], None),
        ):
            output = _capture_output(cmd_suggest_consolidate, self._args())
        assert "Skill A" in output
        assert "Skill B" in output
        assert "--delete-ids 1,2" in output


# ── cmd_claude_skill ─────────────────────────────────────────────────

class TestCmdClaudeSkill:
    def test_installs_skill_to_claude_dir(self, test_db, tmp_path, monkeypatch):
        from src.cli.commands.bootstrap import cmd_claude_skill

        monkeypatch.setenv("HOME", str(tmp_path))

        class Args:
            pass

        output = _capture_output(cmd_claude_skill, Args())
        dest = tmp_path / ".claude" / "skills" / "engram-memory" / "SKILL.md"
        assert dest.is_file()
        assert "Installed Claude Code skill" in output
        content = dest.read_text()
        assert content.startswith("---")
        assert "engram search" in content


# ── cmd_install / import-claude-memories ─────────────────────────────

class TestCmdInstall:
    def test_detects_and_installs_all_present_tools(self, test_db, tmp_path, monkeypatch):
        from src.cli.commands.bootstrap import cmd_install

        for d in (".cursor", ".claude", ".gemini"):
            (tmp_path / d).mkdir()
        monkeypatch.setenv("HOME", str(tmp_path))

        class Args:
            all = False

        output = _capture_output(cmd_install, Args())
        assert (tmp_path / ".cursor" / "mcp.json").is_file()
        assert (tmp_path / ".claude" / "skills" / "engram-memory" / "SKILL.md").is_file()
        assert (tmp_path / ".gemini" / "AGENTS.md").is_file()
        assert "Cursor" in output and "Claude Code" in output and "Antigravity" in output

    def test_skips_undetected_tools(self, test_db, tmp_path, monkeypatch):
        from src.cli.commands.bootstrap import cmd_install

        (tmp_path / ".claude").mkdir()
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setattr("shutil.which", lambda _: None)

        class Args:
            all = False

        output = _capture_output(cmd_install, Args())
        assert not (tmp_path / ".cursor" / "mcp.json").exists()
        assert (tmp_path / ".claude" / "skills" / "engram-memory" / "SKILL.md").is_file()
        assert "not detected" in output


class TestImportClaudeMemories:
    def _write_memory(self, home, name, body):
        mem_dir = home / ".claude" / "projects" / "-users-x-proj" / "memory"
        mem_dir.mkdir(parents=True, exist_ok=True)
        (mem_dir / f"{name}.md").write_text(body)

    def test_import_is_idempotent(self, test_db, tmp_path, monkeypatch):
        from src.cli.commands.bootstrap import cmd_import_claude_memories

        self._write_memory(
            tmp_path,
            "wal-mode",
            "---\nname: wal-mode\ndescription: Always enable WAL\n---\n\nWAL avoids writer starvation.",
        )

        class Args:
            dir = str(tmp_path / ".claude")

        out1 = _capture_output(cmd_import_claude_memories, Args())
        out2 = _capture_output(cmd_import_claude_memories, Args())
        assert "Imported 1" in out1
        assert "Imported 0" in out2 and "1 already present" in out2

    def test_memory_index_file_is_skipped(self, test_db, tmp_path):
        from src.cli.commands.bootstrap import cmd_import_claude_memories

        mem_dir = tmp_path / ".claude" / "projects" / "-p" / "memory"
        mem_dir.mkdir(parents=True)
        (mem_dir / "MEMORY.md").write_text("- [a](a.md) — index only")

        class Args:
            dir = str(tmp_path / ".claude")

        out = _capture_output(cmd_import_claude_memories, Args())
        assert "Imported 0" in out


class TestRegisterClaudeMcp:
    def test_registers_and_is_idempotent(self, test_db, tmp_path):
        import json

        from src.cli.commands.bootstrap import register_claude_mcp

        ok, msg = register_claude_mcp(home=str(tmp_path))
        assert ok and "registered" in msg
        cfg = json.loads((tmp_path / ".claude.json").read_text())
        assert "engram" in cfg["mcpServers"]
        assert cfg["mcpServers"]["engram"]["args"][0].endswith("src/mcp_server.py")

        ok2, msg2 = register_claude_mcp(home=str(tmp_path))
        assert ok2 and "skipped" in msg2

    def test_preserves_existing_config(self, test_db, tmp_path):
        import json

        from src.cli.commands.bootstrap import register_claude_mcp

        (tmp_path / ".claude.json").write_text(json.dumps({"theme": "dark", "mcpServers": {"other": {}}}))
        register_claude_mcp(home=str(tmp_path))
        cfg = json.loads((tmp_path / ".claude.json").read_text())
        assert cfg["theme"] == "dark"
        assert "other" in cfg["mcpServers"] and "engram" in cfg["mcpServers"]
