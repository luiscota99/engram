"""Coverage tests for src/cli/commands/sync.py (skill export/import/sync commands)."""
from __future__ import annotations

import io
import json
import os
import sys
from types import SimpleNamespace

import pytest

from src.database import get_connection, index_in_fts, link_tags

# ── Helpers ──────────────────────────────────────────────────────────

def _capture_output(func, *args, **kwargs) -> str:
    buf = io.StringIO()
    old_stdout = sys.stdout
    sys.stdout = buf
    try:
        func(*args, **kwargs)
    finally:
        sys.stdout = old_stdout
    return buf.getvalue()


def _seed_skill(db_path: str, name: str, domain: str = "engineering", usage: int = 0) -> int:
    with get_connection(db_path) as conn:
        cursor = conn.execute(
            "INSERT INTO skills (name, domain, trigger_desc, workflow, usage_count) "
            "VALUES (?, ?, ?, ?, ?)",
            (name, domain, "When you need to " + name, "Step 1\nStep 2", usage),
        )
        sid = cursor.lastrowid
        link_tags(conn, "skill", sid, ["test"])
        index_in_fts(conn, "skill", sid, name, "trigger | workflow", ["test"])
    return sid


def _seed_pattern(db_path: str, name: str) -> int:
    with get_connection(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO patterns (name, symptoms, root_cause, standard_fix) VALUES (?, ?, ?, ?)",
            (name, "it breaks", "bad state", "reset it"),
        )
        pid = cur.lastrowid
        conn.execute(
            "INSERT INTO pattern_occurrences (pattern_id, conversation_id, date, notes) "
            "VALUES (?, ?, ?, ?)",
            (pid, "conv-1", "2026-01-01", "seen here"),
        )
    return pid


def _write_cursor_skill(base_dir, slug: str, name: str, body: str = "Do the thing.") -> str:
    d = os.path.join(str(base_dir), slug)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "SKILL.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"---\nname: {name}\ndescription: A skill about {name}\n---\n\n{body}\n")
    return path


# ── cmd_export_skills ────────────────────────────────────────────────

class TestCmdExportSkills:
    def _args(self, tmp_path, **overrides):
        base = dict(
            output=str(tmp_path / "out"),
            project_skills=False,
            ids=None,
            domain=None,
            min_usage=0,
            from_patterns=False,
            dry_run=False,
        )
        base.update(overrides)
        return SimpleNamespace(**base)

    def test_no_skills_prints_no_match(self, test_db, tmp_path):
        from src.cli.commands.sync import cmd_export_skills

        out = _capture_output(cmd_export_skills, self._args(tmp_path))
        assert "No skills matched" in out

    def test_dry_run_lists_skill_without_writing(self, test_db, tmp_path):
        from src.cli.commands.sync import cmd_export_skills

        _seed_skill(test_db["path"], "Alpha Skill")
        out = _capture_output(cmd_export_skills, self._args(tmp_path, dry_run=True))
        assert "Dry-run" in out
        assert "Alpha Skill" in out
        assert not (tmp_path / "out" / "alpha-skill" / "SKILL.md").exists()

    def test_export_creates_file_on_disk(self, test_db, tmp_path):
        from src.cli.commands.sync import cmd_export_skills

        _seed_skill(test_db["path"], "Beta Skill")
        out = _capture_output(cmd_export_skills, self._args(tmp_path))
        assert "Export complete" in out
        assert "✓" in out
        skill_file = tmp_path / "out" / "beta-skill" / "SKILL.md"
        assert skill_file.is_file()
        assert "Beta Skill" in skill_file.read_text()

    def test_second_export_reports_skipped(self, test_db, tmp_path):
        from src.cli.commands.sync import cmd_export_skills

        _seed_skill(test_db["path"], "Gamma Skill")
        _capture_output(cmd_export_skills, self._args(tmp_path))
        out2 = _capture_output(cmd_export_skills, self._args(tmp_path))
        assert "Skipped 1 already-existing" in out2
        assert "Nothing new to export" in out2

    def test_ids_filter_limits_export(self, test_db, tmp_path):
        from src.cli.commands.sync import cmd_export_skills

        keep = _seed_skill(test_db["path"], "Keep Skill")
        _seed_skill(test_db["path"], "Drop Skill")
        out = _capture_output(cmd_export_skills, self._args(tmp_path, ids=str(keep), dry_run=True))
        assert "Keep Skill" in out
        assert "Drop Skill" not in out

    def test_project_skills_targets_cwd_cursor_dir(self, test_db, tmp_path, monkeypatch):
        from src.cli.commands.sync import cmd_export_skills

        _seed_skill(test_db["path"], "Proj Skill")
        monkeypatch.chdir(tmp_path)
        out = _capture_output(
            cmd_export_skills, self._args(tmp_path, project_skills=True, dry_run=True)
        )
        assert os.path.join(".cursor", "skills") in out

    def test_from_patterns_shows_pattern_badge(self, test_db, tmp_path):
        from src.cli.commands.sync import cmd_export_skills

        _seed_pattern(test_db["path"], "Deadlock Pattern")
        out = _capture_output(
            cmd_export_skills, self._args(tmp_path, from_patterns=True, dry_run=True)
        )
        assert "Deadlock Pattern" in out
        assert "[pattern]" in out


# ── cmd_import_cursor_skills ─────────────────────────────────────────

class TestCmdImportCursorSkills:
    def test_no_skill_files_found(self, test_db, tmp_path):
        from src.cli.commands.sync import cmd_import_cursor_skills

        empty = tmp_path / "empty"
        empty.mkdir()
        out = _capture_output(
            cmd_import_cursor_skills, SimpleNamespace(path=str(empty), dry_run=False)
        )
        assert "No SKILL.md files found" in out

    def test_dry_run_lists_importable(self, test_db, tmp_path):
        from src.cli.commands.sync import cmd_import_cursor_skills

        _write_cursor_skill(tmp_path, "cool-skill", "Cool Skill")
        out = _capture_output(
            cmd_import_cursor_skills, SimpleNamespace(path=str(tmp_path), dry_run=True)
        )
        assert "Dry-run" in out
        assert "Cool Skill" in out

    def test_dry_run_reports_already_present(self, test_db, tmp_path):
        from src.cli.commands.sync import cmd_import_cursor_skills

        _seed_skill(test_db["path"], "Already Here")
        _write_cursor_skill(tmp_path, "already-here", "Already Here")
        out = _capture_output(
            cmd_import_cursor_skills, SimpleNamespace(path=str(tmp_path), dry_run=True)
        )
        assert "already exist in Engram" in out

    def test_import_creates_skill_in_db(self, test_db, tmp_path):
        from src.cli.commands.sync import cmd_import_cursor_skills

        _write_cursor_skill(tmp_path, "real-skill", "Real Skill")
        out = _capture_output(
            cmd_import_cursor_skills, SimpleNamespace(path=str(tmp_path), dry_run=False)
        )
        assert "Import complete" in out
        assert "Real Skill" in out
        assert "Skill #" in out
        with get_connection(test_db["path"]) as conn:
            row = conn.execute("SELECT domain FROM skills WHERE name = ?", ("Real Skill",)).fetchone()
        assert row is not None

    def test_reimport_reports_skipped(self, test_db, tmp_path):
        from src.cli.commands.sync import cmd_import_cursor_skills

        _write_cursor_skill(tmp_path, "dup-skill", "Dup Skill")
        args = SimpleNamespace(path=str(tmp_path), dry_run=False)
        _capture_output(cmd_import_cursor_skills, args)
        out2 = _capture_output(cmd_import_cursor_skills, args)
        assert "Skipped 1 skill(s) already in Engram" in out2
        assert "Nothing new imported" in out2

    def test_dry_run_reports_unparseable(self, test_db, tmp_path):
        from src.cli.commands.sync import cmd_import_cursor_skills

        # A SKILL.md whose frontmatter has no name → parse_failed
        d = tmp_path / "bad"
        d.mkdir()
        (d / "SKILL.md").write_text("---\ntitle: nameless\n---\n\nbody\n")
        out = _capture_output(
            cmd_import_cursor_skills, SimpleNamespace(path=str(tmp_path), dry_run=True)
        )
        assert "could not be parsed" in out

    def test_import_reports_parse_failures(self, test_db, tmp_path):
        from src.cli.commands.sync import cmd_import_cursor_skills

        d = tmp_path / "bad"
        d.mkdir()
        (d / "SKILL.md").write_text("no frontmatter at all\n")
        out = _capture_output(
            cmd_import_cursor_skills, SimpleNamespace(path=str(tmp_path), dry_run=False)
        )
        assert "failed to parse" in out


# ── cmd_sync_skills ──────────────────────────────────────────────────

class TestCmdSyncSkills:
    def _args(self, path, **overrides):
        base = dict(path=path, dry_run=False, auto=False, export_missing=False, import_missing=False)
        base.update(overrides)
        return SimpleNamespace(**base)

    def test_summary_lists_only_in_engram(self, test_db, tmp_path):
        from src.cli.commands.sync import cmd_sync_skills

        _seed_skill(test_db["path"], "Engram Only", usage=7)
        empty = tmp_path / "cursor"
        empty.mkdir()
        out = _capture_output(cmd_sync_skills, self._args(str(empty)))
        assert "Only in Engram" in out
        assert "can export" in out
        assert "Engram Only" in out
        assert "usage:7" in out
        assert "Run with --auto" in out

    def test_summary_lists_only_in_cursor(self, test_db, tmp_path):
        from src.cli.commands.sync import cmd_sync_skills

        cursor = tmp_path / "cursor"
        cursor.mkdir()
        _write_cursor_skill(cursor, "cursor-only", "Cursor Only")
        out = _capture_output(cmd_sync_skills, self._args(str(cursor)))
        assert "Only in Cursor" in out
        assert "can import" in out
        assert "cursor-only" in out

    def test_dry_run_suppresses_action_hint(self, test_db, tmp_path):
        from src.cli.commands.sync import cmd_sync_skills

        cursor = tmp_path / "cursor"
        cursor.mkdir()
        out = _capture_output(cmd_sync_skills, self._args(str(cursor), dry_run=True))
        assert "Run with --auto" not in out

    def test_auto_exports_missing_to_cursor(self, test_db, tmp_path):
        from src.cli.commands.sync import cmd_sync_skills

        _seed_skill(test_db["path"], "Sync Export")
        cursor = tmp_path / "cursor"
        cursor.mkdir()
        out = _capture_output(cmd_sync_skills, self._args(str(cursor), auto=True))
        assert "Exported 1 skill(s) to Cursor" in out
        assert (cursor / "sync-export" / "SKILL.md").is_file()

    def test_auto_imports_missing_into_engram(self, test_db, tmp_path):
        from src.cli.commands.sync import cmd_sync_skills

        cursor = tmp_path / "cursor"
        cursor.mkdir()
        _write_cursor_skill(cursor, "sync-import", "Sync Import")
        out = _capture_output(cmd_sync_skills, self._args(str(cursor), auto=True))
        assert "Imported 1 skill(s) into Engram" in out
        with get_connection(test_db["path"]) as conn:
            row = conn.execute("SELECT id FROM skills WHERE name = ?", ("Sync Import",)).fetchone()
        assert row is not None


# ── cmd_import_skills (legacy orchestrator format) ───────────────────

class TestCmdImportSkills:
    def _write_legacy(self, base, slug, frontmatter, body=""):
        d = os.path.join(str(base), slug)
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, "SKILL.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"---\n{frontmatter}\n---\n{body}")
        return path

    def test_directory_not_found_exits(self, test_db, tmp_path):
        from src.cli.commands.sync import cmd_import_skills

        args = SimpleNamespace(path=str(tmp_path / "nope"))
        with pytest.raises(SystemExit):
            _capture_output(cmd_import_skills, args)

    def test_no_skill_files_exits(self, test_db, tmp_path):
        from src.cli.commands.sync import cmd_import_skills

        d = tmp_path / "empty"
        d.mkdir()
        args = SimpleNamespace(path=str(d))
        with pytest.raises(SystemExit):
            _capture_output(cmd_import_skills, args)

    def test_imports_legacy_skill_and_classifies_domain(self, test_db, tmp_path):
        from src.cli.commands.sync import cmd_import_skills

        self._write_legacy(
            tmp_path,
            "react-helper",
            "name: React Helper\ndescription: helps with ui",
            "\n## When to Use\n- building components\n- styling\n\nMore text.",
        )
        out = _capture_output(cmd_import_skills, SimpleNamespace(path=str(tmp_path)))
        assert "Imported 1 skills" in out
        with get_connection(test_db["path"]) as conn:
            row = conn.execute(
                "SELECT domain, trigger_desc FROM skills WHERE name = ?", ("React Helper",)
            ).fetchone()
        assert row["domain"] == "frontend"
        assert "building components" in row["trigger_desc"]

    def test_skips_file_without_frontmatter(self, test_db, tmp_path):
        from src.cli.commands.sync import cmd_import_skills

        d = os.path.join(str(tmp_path), "plain")
        os.makedirs(d)
        with open(os.path.join(d, "SKILL.md"), "w", encoding="utf-8") as f:
            f.write("just prose, no frontmatter\n")
        out = _capture_output(cmd_import_skills, SimpleNamespace(path=str(tmp_path)))
        assert "Imported 0 skills, skipped 1" in out

    def test_skips_already_existing_skill(self, test_db, tmp_path):
        from src.cli.commands.sync import cmd_import_skills

        self._write_legacy(tmp_path, "dupe", "name: Dupe Skill\ndescription: a thing", "\nBody here.")
        args = SimpleNamespace(path=str(tmp_path))
        _capture_output(cmd_import_skills, args)
        out2 = _capture_output(cmd_import_skills, args)
        assert "Imported 0 skills, skipped 1" in out2

    def test_multiline_description_and_tags_persisted(self, test_db, tmp_path):
        from src.cli.commands.sync import cmd_import_skills

        self._write_legacy(
            tmp_path,
            "sec-skill",
            "name: Security Auditor\ndescription: >-\n  audits code\n  for security holes",
            "\nDetailed body.",
        )
        _capture_output(cmd_import_skills, SimpleNamespace(path=str(tmp_path)))
        with get_connection(test_db["path"]) as conn:
            row = conn.execute(
                "SELECT domain, dependencies, key_files FROM skills WHERE name = ?",
                ("Security Auditor",),
            ).fetchone()
        assert row["domain"] == "security"
        assert row["dependencies"] == "ks-cursor-orchestrator"
        assert json.loads(row["key_files"])[0].endswith("SKILL.md")
