"""Coverage tests for src/cli/commands/bootstrap.py.

All tests are hermetic: HOME is redirected per-test via monkeypatch so no
real ~/.gemini, ~/.claude, or ~/.cursor files are touched, and cwd-writing
commands run under a tmp working directory.
"""
from __future__ import annotations

import io
import json
import os
import sys
from datetime import date

import pytest

from src.cli.commands import bootstrap as bs
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


class _FakeResp:
    def __init__(self, status: int):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _fake_urlopen_ok(*_a, **_k):
    return _FakeResp(200)


def _fake_urlopen_fail(*_a, **_k):
    raise OSError("connection refused")


# ── _omit_project_integration_files ──────────────────────────────────

class TestOmitProjectIntegration:
    def test_true_when_arg_flag_set(self, tmp_path):
        class Args:
            omit_project_integration = True

        assert bs._omit_project_integration_files(str(tmp_path), Args()) is True

    def test_true_when_sentinel_present(self, tmp_path):
        (tmp_path / bs._OMIT_PROJECT_INTEGRATION_SENTINEL).write_text("")

        class Args:
            omit_project_integration = False

        assert bs._omit_project_integration_files(str(tmp_path), Args()) is True

    def test_false_when_neither(self, tmp_path):
        class Args:
            pass

        assert bs._omit_project_integration_files(str(tmp_path), Args()) is False


# ── global antigravity snippet ───────────────────────────────────────

class TestGlobalAntigravitySnippet:
    def test_body_mentions_engram_cli(self):
        body = bs._global_antigravity_agents_body()
        assert "engram" in body
        assert "~/.engram/memory.db" in body

    def test_writes_fresh_file(self, tmp_path):
        ok, path = bs.write_global_antigravity_agents_snippet(home=str(tmp_path))
        assert ok is True
        assert path == str(tmp_path / ".gemini" / "AGENTS.md")
        content = (tmp_path / ".gemini" / "AGENTS.md").read_text()
        assert bs._GLOBAL_AGENTS_BEGIN in content
        assert bs._GLOBAL_AGENTS_END in content
        assert "global engineering memory" in content

    def test_appends_to_existing_unrelated_content(self, tmp_path):
        gemini = tmp_path / ".gemini"
        gemini.mkdir()
        (gemini / "AGENTS.md").write_text("# My own rules\n\nKeep these.\n")

        ok, _ = bs.write_global_antigravity_agents_snippet(home=str(tmp_path))
        content = (gemini / "AGENTS.md").read_text()
        assert ok is True
        assert "My own rules" in content
        assert bs._GLOBAL_AGENTS_BEGIN in content

    def test_idempotent_replaces_existing_block(self, tmp_path):
        bs.write_global_antigravity_agents_snippet(home=str(tmp_path))
        bs.write_global_antigravity_agents_snippet(home=str(tmp_path))
        content = (tmp_path / ".gemini" / "AGENTS.md").read_text()
        # Block markers should appear exactly once after a re-run.
        assert content.count(bs._GLOBAL_AGENTS_BEGIN) == 1
        assert content.count(bs._GLOBAL_AGENTS_END) == 1


# ── cmd_antigravity_global ───────────────────────────────────────────

class TestCmdAntigravityGlobal:
    def test_updates_rules_and_prints_path(self, test_db, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))

        out = _capture(bs.cmd_antigravity_global, object())
        assert "Updated Antigravity global rules" in out
        assert (home / ".gemini" / "AGENTS.md").is_file()

    def test_initializes_db_when_missing(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("ENGRAM_DB_PATH", str(tmp_path / "fresh.db"))

        out = _capture(bs.cmd_antigravity_global, object())
        assert "Initialized database" in out
        assert (tmp_path / "fresh.db").is_file()


# ── install_claude_skill / cmd_claude_skill ──────────────────────────

class TestInstallClaudeSkill:
    def test_copies_skill_into_home(self, tmp_path):
        home = tmp_path / "home"
        home.mkdir()
        ok, dest = bs.install_claude_skill(home=str(home))
        assert ok is True
        assert dest == str(home / ".claude" / "skills" / "engram-memory" / "SKILL.md")
        assert os.path.isfile(dest)

    def test_missing_source_returns_error(self, tmp_path, monkeypatch):
        # Point the engram root at an empty dir so the skill source is absent.
        monkeypatch.setattr(bs, "_engram_root", lambda: str(tmp_path / "empty"))
        ok, msg = bs.install_claude_skill(home=str(tmp_path))
        assert ok is False
        assert "skill source not found" in msg


class TestCmdClaudeSkill:
    def test_installs_and_prints_confirmation(self, test_db, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        out = _capture(bs.cmd_claude_skill, object())
        assert "Installed Claude Code skill" in out
        assert (home / ".claude" / "skills" / "engram-memory" / "SKILL.md").is_file()

    def test_exits_when_install_fails(self, test_db, monkeypatch):
        monkeypatch.setattr(bs, "install_claude_skill", lambda: (False, "boom"))
        with pytest.raises(SystemExit) as exc:
            _capture(bs.cmd_claude_skill, object())
        assert exc.value.code == 1

    def test_initializes_db_when_missing(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("ENGRAM_DB_PATH", str(tmp_path / "fresh.db"))
        out = _capture(bs.cmd_claude_skill, object())
        assert "Initialized database" in out
        assert "Installed Claude Code skill" in out


# ── register_claude_mcp ──────────────────────────────────────────────

class TestRegisterClaudeMcp:
    def test_registers_fresh_entry(self, tmp_path):
        ok, msg = bs.register_claude_mcp(home=str(tmp_path))
        assert ok is True
        assert "registered" in msg
        cfg = json.loads((tmp_path / ".claude.json").read_text())
        assert cfg["mcpServers"]["engram"]["type"] == "stdio"
        assert cfg["mcpServers"]["engram"]["command"] == sys.executable

    def test_skips_when_already_present(self, tmp_path):
        (tmp_path / ".claude.json").write_text(
            json.dumps({"mcpServers": {"engram": {"type": "stdio"}}})
        )
        ok, msg = bs.register_claude_mcp(home=str(tmp_path))
        assert ok is True
        assert "already registered" in msg

    def test_returns_warning_on_bad_json(self, tmp_path):
        (tmp_path / ".claude.json").write_text("{not valid json")
        ok, msg = bs.register_claude_mcp(home=str(tmp_path))
        assert ok is False
        assert "could not register" in msg


# ── detect_integrations ──────────────────────────────────────────────

class TestDetectIntegrations:
    def test_detects_cursor_dir(self, tmp_path, monkeypatch):
        (tmp_path / ".cursor").mkdir()
        # Ensure `claude`/`antigravity` binaries don't leak in from PATH.
        monkeypatch.setattr(bs.shutil, "which", lambda _n: None)
        result = bs.detect_integrations(home=str(tmp_path))
        assert result["cursor"] is True
        assert result["claude"] is False
        assert result["antigravity"] is False

    def test_detects_claude_and_gemini_dirs(self, tmp_path, monkeypatch):
        (tmp_path / ".claude").mkdir()
        (tmp_path / ".gemini").mkdir()
        monkeypatch.setattr(bs.shutil, "which", lambda _n: None)
        result = bs.detect_integrations(home=str(tmp_path))
        assert result["claude"] is True
        assert result["antigravity"] is True
        assert result["cursor"] is False


# ── cmd_install ──────────────────────────────────────────────────────

class TestCmdInstall:
    def test_no_targets_detected(self, test_db, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setattr(bs.shutil, "which", lambda _n: None)

        class Args:
            all = False

        out = _capture(bs.cmd_install, Args())
        assert "No supported tools detected" in out
        assert "install --all" in out

    def test_all_flag_sets_up_everything(self, test_db, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setattr(bs.shutil, "which", lambda _n: None)

        class Args:
            all = True

        out = _capture(bs.cmd_install, Args())
        assert "Engram install" in out
        assert "Cursor:" in out
        assert "Claude Code:" in out
        assert "Antigravity:" in out
        # Antigravity global rules should have been written.
        assert (home / ".gemini" / "AGENTS.md").is_file()

    def test_partial_detection_marks_skipped(self, test_db, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        (home / ".cursor").mkdir()  # only cursor present
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setattr(bs.shutil, "which", lambda _n: None)

        class Args:
            all = False

        out = _capture(bs.cmd_install, Args())
        assert "Cursor:" in out
        assert "not detected — skipped" in out

    def test_claude_only_skill_failure_warns(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        (home / ".claude").mkdir()  # only claude present → cursor branch skipped
        monkeypatch.setenv("HOME", str(home))
        # Fresh DB path so cmd_install also runs the init_db branch.
        monkeypatch.setenv("ENGRAM_DB_PATH", str(tmp_path / "fresh.db"))
        monkeypatch.setattr(bs.shutil, "which", lambda _n: None)
        monkeypatch.setattr(bs, "install_claude_skill", lambda: (False, "no source"))

        class Args:
            all = False

        out = _capture(bs.cmd_install, Args())
        assert "Initialized database" in out
        assert "Claude Code: Warning — no source" in out
        # Cursor was not a target → listed as skipped, no "Cursor:" setup line.
        assert "Cursor: not detected" in out


# ── _iter_claude_memory_files ────────────────────────────────────────

class TestIterClaudeMemoryFiles:
    def test_yields_md_but_skips_index(self, tmp_path):
        mem = tmp_path / "proj" / "memory"
        mem.mkdir(parents=True)
        (mem / "note-one.md").write_text("a")
        (mem / "MEMORY.md").write_text("index")
        (mem / "not-markdown.txt").write_text("x")
        (tmp_path / "proj" / "outside.md").write_text("ignored")

        found = list(bs._iter_claude_memory_files(str(tmp_path)))
        assert len(found) == 1
        assert found[0].endswith("note-one.md")


# ── cmd_import_claude_memories ───────────────────────────────────────

class TestCmdImportClaudeMemories:
    def _make_memory_dir(self, tmp_path):
        mem = tmp_path / "claudehome" / "projX" / "memory"
        mem.mkdir(parents=True)
        return tmp_path / "claudehome", mem

    def test_imports_memories_and_is_idempotent(self, test_db, tmp_path):
        chome, mem = self._make_memory_dir(tmp_path)
        (mem / "lesson.md").write_text("Real memory body content.")

        class Args:
            dir = str(chome)

        out1 = _capture(bs.cmd_import_claude_memories, Args())
        assert "Imported 1 Claude Code memories (0 already present)" in out1

        with get_connection(test_db["path"]) as conn:
            row = conn.execute(
                "SELECT title, domain, key_decisions FROM conversations "
                "WHERE domain = 'claude-memory'"
            ).fetchone()
        assert row is not None
        assert row["title"] == "lesson"
        assert "Real memory body content." in row["key_decisions"]

        # Second run: same content hash → skipped, nothing new imported.
        out2 = _capture(bs.cmd_import_claude_memories, Args())
        assert "Imported 0 Claude Code memories (1 already present)" in out2

    def test_lifts_title_from_frontmatter(self, test_db, tmp_path):
        chome, mem = self._make_memory_dir(tmp_path)
        (mem / "raw-name.md").write_text(
            "---\ndescription: Friendly Title\n---\nbody text here\n"
        )

        class Args:
            dir = str(chome)

        _capture(bs.cmd_import_claude_memories, Args())
        with get_connection(test_db["path"]) as conn:
            row = conn.execute(
                "SELECT title FROM conversations WHERE domain = 'claude-memory'"
            ).fetchone()
        assert row["title"] == "Friendly Title"

    def test_skips_empty_files_and_scans_frontmatter(self, test_db, tmp_path):
        chome, mem = self._make_memory_dir(tmp_path)
        (mem / "blank.md").write_text("   \n  ")  # empty after strip → skipped
        # Frontmatter with a non-description line first (exercises the scan loop)
        # and no closing delimiter within the window.
        (mem / "meta.md").write_text(
            "---\ntags: x\nauthor: me\ndescription: Lifted Title\nmore: stuff\n"
        )

        class Args:
            dir = str(chome)

        out = _capture(bs.cmd_import_claude_memories, Args())
        assert "Imported 1 Claude Code memories" in out
        with get_connection(test_db["path"]) as conn:
            row = conn.execute(
                "SELECT title FROM conversations WHERE domain = 'claude-memory'"
            ).fetchone()
        assert row["title"] == "Lifted Title"

    def test_missing_dir_exits(self, test_db, tmp_path):
        class Args:
            dir = str(tmp_path / "does-not-exist")

        with pytest.raises(SystemExit) as exc:
            bs.cmd_import_claude_memories(Args())
        assert exc.value.code == 1

    def test_no_memory_files_prints_hint(self, test_db, tmp_path):
        empty = tmp_path / "empty-home"
        empty.mkdir()

        class Args:
            dir = str(empty)

        out = _capture(bs.cmd_import_claude_memories, Args())
        assert "Imported 0 Claude Code memories (0 already present)" in out
        assert "No memory files found" in out


# ── _date_today ──────────────────────────────────────────────────────

def test_date_today_matches_isoformat():
    assert bs._date_today() == date.today().isoformat()


# ── _prompt_bootstrap_mode ───────────────────────────────────────────

class TestPromptBootstrapMode:
    @pytest.mark.parametrize(
        "raw,expected",
        [("", "adaptive"), ("1", "adaptive"), ("2", "full"), ("3", "minimal")],
    )
    def test_direct_choices(self, raw, expected, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda _p: raw)
        assert _capture_return(bs._prompt_bootstrap_mode) == expected

    def test_reprompts_on_invalid_then_valid(self, monkeypatch):
        answers = iter(["9", "2"])
        monkeypatch.setattr("builtins.input", lambda _p: next(answers))
        buf = io.StringIO()
        old = sys.stdout
        sys.stdout = buf
        try:
            result = bs._prompt_bootstrap_mode()
        finally:
            sys.stdout = old
        assert result == "full"
        assert "Please enter 1, 2, or 3" in buf.getvalue()

    def test_eof_defaults_to_adaptive(self, monkeypatch):
        def _raise(_p):
            raise EOFError

        monkeypatch.setattr("builtins.input", _raise)
        buf = io.StringIO()
        old = sys.stdout
        sys.stdout = buf
        try:
            result = bs._prompt_bootstrap_mode()
        finally:
            sys.stdout = old
        assert result == "adaptive"
        assert "Non-interactive environment" in buf.getvalue()


def _capture_return(func):
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        return func()
    finally:
        sys.stdout = old


# ── _setup_mcp_config ────────────────────────────────────────────────

class TestSetupMcpConfig:
    def test_adds_engram_entry(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        ok, msg = bs._setup_mcp_config(str(tmp_path / "engram_root"))
        assert ok is True
        assert "Added Engram MCP server" in msg
        cfg = json.loads((home / ".cursor" / "mcp.json").read_text())
        assert "engram" in cfg["mcpServers"]
        assert cfg["mcpServers"]["engram"]["enabled"] is True

    def test_skips_when_already_present(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        cursor = home / ".cursor"
        cursor.mkdir(parents=True)
        (cursor / "mcp.json").write_text(json.dumps({"mcpServers": {"engram": {}}}))
        monkeypatch.setenv("HOME", str(home))
        ok, msg = bs._setup_mcp_config(str(tmp_path / "engram_root"))
        assert ok is True
        assert "already has 'engram' entry" in msg

    def test_warns_on_bad_json(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        cursor = home / ".cursor"
        cursor.mkdir(parents=True)
        (cursor / "mcp.json").write_text("{broken")
        monkeypatch.setenv("HOME", str(home))
        ok, msg = bs._setup_mcp_config(str(tmp_path / "engram_root"))
        assert ok is False
        assert "Could not update MCP config" in msg


# ── cmd_bootstrap ────────────────────────────────────────────────────

class TestCmdBootstrap:
    def _prep(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        work = tmp_path / "work"
        work.mkdir()
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.chdir(work)
        return home, work

    def test_full_mode_creates_project_files(self, test_db, tmp_path, monkeypatch):
        home, work = self._prep(tmp_path, monkeypatch)
        monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen_ok)

        class Args:
            mode = "full"
            omit_project_integration = False
            setup_mcp = True
            global_antigravity = True

        out = _capture(bs.cmd_bootstrap, Args())
        assert "FULL" in out
        assert "successfully bootstrapped" in out
        assert "Ollama reachable" in out
        # Project files written under the temp cwd.
        assert (work / ".cursor" / "rules" / "engram.mdc").is_file()
        ag = (work / ".antigravity" / "instructions.md").read_text()
        assert "FULL" in ag
        # MCP config written under redirected HOME.
        assert (home / ".cursor" / "mcp.json").is_file()
        # --global-antigravity wrote the global block.
        assert (home / ".gemini" / "AGENTS.md").is_file()

    def test_minimal_mode_writes_disabled_instructions(self, test_db, tmp_path, monkeypatch):
        home, work = self._prep(tmp_path, monkeypatch)
        monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen_fail)

        class Args:
            mode = "minimal"
            omit_project_integration = False
            setup_mcp = False
            global_antigravity = False

        out = _capture(bs.cmd_bootstrap, Args())
        assert "MINIMAL" in out
        assert "Memory is disabled by default" in out
        assert "Ollama not reachable" in out
        assert "Skipped MCP setup" in out
        ag = (work / ".antigravity" / "instructions.md").read_text()
        assert "off by default" in ag

    def test_omit_project_skips_workspace_files(self, test_db, tmp_path, monkeypatch):
        home, work = self._prep(tmp_path, monkeypatch)
        monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen_fail)

        class Args:
            mode = None  # omit path defaults to adaptive
            omit_project_integration = True
            setup_mcp = False
            global_antigravity = False

        out = _capture(bs.cmd_bootstrap, Args())
        assert "(skipped) .cursor/rules/engram.mdc" in out
        assert not (work / ".cursor").exists()
        assert not (work / ".antigravity").exists()

    def test_adaptive_mode_via_prompt_and_mcp_autodetect(self, test_db, tmp_path, monkeypatch):
        home, work = self._prep(tmp_path, monkeypatch)
        monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen_fail)
        # mode=None + not omit → interactive mode prompt is consulted.
        monkeypatch.setattr(bs, "_prompt_bootstrap_mode", lambda: "adaptive")

        class Args:
            mode = None
            omit_project_integration = False
            setup_mcp = None  # non-tty test env → auto-enabled
            global_antigravity = False

        out = _capture(bs.cmd_bootstrap, Args())
        assert "ADAPTIVE" in out
        assert "LIGHT mode by default" in out
        # setup_mcp resolved to True in a non-interactive env.
        assert (home / ".cursor" / "mcp.json").is_file()

    def test_interactive_mcp_decline(self, test_db, tmp_path, monkeypatch):
        home, work = self._prep(tmp_path, monkeypatch)
        monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen_fail)

        class _Tty:
            @staticmethod
            def isatty():
                return True

        monkeypatch.setattr(sys, "stdin", _Tty())
        monkeypatch.setattr("builtins.input", lambda _p: "n")

        class Args:
            mode = "adaptive"
            omit_project_integration = False
            setup_mcp = None
            global_antigravity = False

        out = _capture(bs.cmd_bootstrap, Args())
        assert "Skipped MCP setup" in out
        assert not (home / ".cursor" / "mcp.json").exists()

    def test_initializes_db_when_missing(self, tmp_path, monkeypatch):
        home, work = self._prep(tmp_path, monkeypatch)
        monkeypatch.setenv("ENGRAM_DB_PATH", str(tmp_path / "fresh.db"))
        monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen_fail)

        class Args:
            mode = "adaptive"
            omit_project_integration = True
            setup_mcp = False
            global_antigravity = False

        out = _capture(bs.cmd_bootstrap, Args())
        assert "database not found" in out.lower()
        assert (tmp_path / "fresh.db").is_file()

    def test_unknown_mode_exits(self, test_db, tmp_path, monkeypatch):
        self._prep(tmp_path, monkeypatch)

        class Args:
            mode = "bogus"
            omit_project_integration = False
            setup_mcp = False
            global_antigravity = False

        with pytest.raises(SystemExit) as exc:
            _capture(bs.cmd_bootstrap, Args())
        assert exc.value.code == 1


# ── cmd_seed / cmd_init ──────────────────────────────────────────────

class TestCmdSeed:
    def test_seeds_mistakes_into_db(self, test_db):
        out = _capture(bs.cmd_seed, object())
        assert "Seeding database" in out
        with get_connection(test_db["path"]) as conn:
            count = conn.execute("SELECT COUNT(*) AS c FROM mistakes").fetchone()["c"]
        assert count > 0


class TestCmdInit:
    def test_prints_initialized_path(self, test_db):
        out = _capture(bs.cmd_init, object())
        assert "Database initialized at" in out
        assert test_db["path"] in out
