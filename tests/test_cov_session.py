"""Coverage tests for src/cli/commands/session.py and src/session_review.py."""
from __future__ import annotations

import io
import sys
from unittest.mock import patch

import pytest

from src.database import get_connection

# ── helpers ──────────────────────────────────────────────────────────

def _capture_output(func, *args, **kwargs) -> str:
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        func(*args, **kwargs)
    finally:
        sys.stdout = old
    return buf.getvalue()


def _seed_session(db_path: str, *, session_id="sess-1", with_transcript=True):
    with get_connection(db_path) as conn:
        conn.execute(
            """INSERT INTO sessions
               (session_id, title, date, domain, workflow_used, key_decisions)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (session_id, "Design Review", "2026-07-01", "engineering",
             "committee-v1", "Chose SQLite over Postgres"),
        )
        if with_transcript:
            conn.execute(
                "INSERT INTO session_transcripts (session_id, role, content, timestamp) "
                "VALUES (?, ?, ?, ?)",
                (session_id, "Analyst", "Considered tradeoffs carefully", "2026-07-01T10:00"),
            )


def _seed_role(db_path: str, name="Facilitator"):
    with get_connection(db_path) as conn:
        conn.execute(
            "INSERT INTO roles (name, charter, heuristics) VALUES (?, ?, ?)",
            (name, "Drive consensus", "Ask clarifying questions"),
        )


# ── cmd_get_session ──────────────────────────────────────────────────

class TestCmdGetSession:
    def test_prints_full_session_with_transcripts(self, test_db):
        from src.cli.commands.session import cmd_get_session

        _seed_session(test_db["path"])

        class Args:
            id = "sess-1"

        out = _capture_output(cmd_get_session, Args())
        assert "Design Review" in out
        assert "sess-1" in out
        assert "2026-07-01" in out
        assert "engineering" in out
        assert "committee-v1" in out
        assert "Chose SQLite over Postgres" in out
        assert "Analyst" in out
        assert "Considered tradeoffs carefully" in out

    def test_session_without_optional_fields(self, test_db):
        from src.cli.commands.session import cmd_get_session

        with get_connection(test_db["path"]) as conn:
            conn.execute(
                "INSERT INTO sessions (session_id, title, date, domain) VALUES (?, ?, ?, ?)",
                ("bare", "Bare Session", "2026-07-02", "devops"),
            )

        class Args:
            id = "bare"

        out = _capture_output(cmd_get_session, Args())
        assert "Bare Session" in out
        # no workflow / decisions / transcript sections
        assert "Workflow:" not in out
        assert "Key Decisions:" not in out
        assert "Transcripts:" not in out

    def test_missing_session_exits_nonzero(self, test_db):
        from src.cli.commands.session import cmd_get_session

        class Args:
            id = "nope"

        with pytest.raises(SystemExit) as exc:
            _capture_output(cmd_get_session, Args())
        assert exc.value.code == 1


# ── cmd_get_role ─────────────────────────────────────────────────────

class TestCmdGetRole:
    def test_prints_charter_and_heuristics(self, test_db):
        from src.cli.commands.session import cmd_get_role

        _seed_role(test_db["path"])

        class Args:
            name = "Facilitator"

        out = _capture_output(cmd_get_role, Args())
        assert "Facilitator" in out
        assert "Drive consensus" in out
        assert "Ask clarifying questions" in out

    def test_missing_role_prints_not_found(self, test_db):
        from src.cli.commands.session import cmd_get_role

        class Args:
            name = "Ghost"

        out = _capture_output(cmd_get_role, Args())
        assert "Role 'Ghost' not found." in out


# ── cmd_session_review ───────────────────────────────────────────────

class TestCmdSessionReview:
    def _args(self, **over):
        class Args:
            no_project = True
            project = None
            conversation_id = "conv-abcdef123456"
            tasks = "Shipped feature X"
            bugs_fixed = ""
            new_patterns = ""
            workflows_used = ""

        a = Args()
        for k, v in over.items():
            setattr(a, k, v)
        return a

    def test_no_project_branch(self, test_db):
        from src.cli.commands.session import cmd_session_review

        out = _capture_output(cmd_session_review, self._args())
        assert "Session Retrospective" in out
        assert "Shipped feature X" in out
        assert "conv-abcdef1" in out  # truncated to 12 chars
        assert "Project:" not in out

    def test_explicit_project_branch(self, test_db, tmp_path):
        from src.cli.commands.session import cmd_session_review

        out = _capture_output(
            cmd_session_review,
            self._args(no_project=False, project=str(tmp_path)),
        )
        assert "Project:" in out
        assert tmp_path.name in out

    def test_default_cwd_branch_uses_getcwd(self, test_db):
        from src.cli.commands.session import cmd_session_review

        with patch("src.cli.commands.session.os.getcwd", return_value="/tmp"):
            out = _capture_output(
                cmd_session_review, self._args(no_project=False, project=None)
            )
        assert "Session Retrospective" in out


# ── _parse_session_summary_file ──────────────────────────────────────

class TestParseSessionSummaryFile:
    def test_no_front_matter_returns_body_only(self):
        from src.cli.commands.session import _parse_session_summary_file

        meta, body = _parse_session_summary_file("Just a plain body.")
        assert meta == {}
        assert body == "Just a plain body."

    def test_valid_front_matter_parsed(self):
        from src.cli.commands.session import _parse_session_summary_file

        text = '---\ntitle: "My Title"\ndomain: devops\ntags: a, b\n---\nBody here.'
        meta, body = _parse_session_summary_file(text)
        assert meta["title"] == "My Title"
        assert meta["domain"] == "devops"
        assert meta["tags"] == "a, b"
        assert body == "Body here."

    def test_comments_and_blank_lines_ignored(self):
        from src.cli.commands.session import _parse_session_summary_file

        text = "---\n# a comment\n\ntitle: T\n---\nBody"
        meta, body = _parse_session_summary_file(text)
        assert meta == {"title": "T"}
        assert body == "Body"

    def test_bom_and_malformed_front_matter(self):
        from src.cli.commands.session import _parse_session_summary_file

        # Only one leading "---" and no closing → treated as no front matter
        meta, body = _parse_session_summary_file("﻿--- not really front matter")
        assert meta == {}
        assert body == "--- not really front matter"


# ── _title_from_body ─────────────────────────────────────────────────

class TestTitleFromBody:
    def test_markdown_heading_wins(self):
        from src.cli.commands.session import _title_from_body

        assert _title_from_body("# The Heading\nmore text") == "The Heading"

    def test_first_line_when_no_heading(self):
        from src.cli.commands.session import _title_from_body

        assert _title_from_body("first line\nsecond") == "first line"

    def test_empty_body_falls_back(self):
        from src.cli.commands.session import _title_from_body

        assert _title_from_body("   ") == "Session summary"


# ── cmd_import_session_summary ───────────────────────────────────────

class TestCmdImportSessionSummary:
    def test_imports_and_links_project(self, test_db, tmp_path):
        from src.cli.commands.session import cmd_import_session_summary

        f = tmp_path / "summary.md"
        f.write_text(
            "---\ntitle: Import Me\ndomain: engineering\ntags: rust,gba\n---\n\nDid the work.",
            encoding="utf-8",
        )

        class Args:
            file = str(f)
            project = str(tmp_path)
            force = False

        out = _capture_output(cmd_import_session_summary, Args())
        assert "Imported session summary as conversation" in out
        assert "Project link:" in out
        # Row actually landed in the DB with title/body.
        with get_connection(test_db["path"]) as conn:
            row = conn.execute(
                "SELECT title, tasks_completed, domain FROM conversations"
            ).fetchone()
        assert row["title"] == "Import Me"
        assert row["tasks_completed"] == "Did the work."
        assert row["domain"] == "engineering"

    def test_duplicate_is_skipped(self, test_db, tmp_path):
        from src.cli.commands.session import cmd_import_session_summary

        f = tmp_path / "summary.md"
        f.write_text("Plain body content", encoding="utf-8")

        class Args:
            file = str(f)
            project = str(tmp_path)
            force = False

        _capture_output(cmd_import_session_summary, Args())
        out2 = _capture_output(cmd_import_session_summary, Args())
        assert "Skip:" in out2
        assert "already exists" in out2
        with get_connection(test_db["path"]) as conn:
            n = conn.execute("SELECT COUNT(*) c FROM conversations").fetchone()["c"]
        assert n == 1

    def test_force_inserts_second_copy(self, test_db, tmp_path):
        from src.cli.commands.session import cmd_import_session_summary

        f = tmp_path / "summary.md"
        f.write_text("Plain body content", encoding="utf-8")

        class ArgsNoForce:
            file = str(f)
            project = str(tmp_path)
            force = False

        class ArgsForce:
            file = str(f)
            project = str(tmp_path)
            force = True

        _capture_output(cmd_import_session_summary, ArgsNoForce())
        out = _capture_output(cmd_import_session_summary, ArgsForce())
        assert "Imported session summary as conversation" in out
        with get_connection(test_db["path"]) as conn:
            n = conn.execute("SELECT COUNT(*) c FROM conversations").fetchone()["c"]
        assert n == 2

    def test_missing_file_exits(self, test_db, tmp_path):
        from src.cli.commands.session import cmd_import_session_summary

        class Args:
            file = str(tmp_path / "does_not_exist.md")
            project = None
            force = False

        with pytest.raises(SystemExit) as exc:
            _capture_output(cmd_import_session_summary, Args())
        assert exc.value.code == 1

    def test_empty_body_exits(self, test_db, tmp_path):
        from src.cli.commands.session import cmd_import_session_summary

        f = tmp_path / "empty.md"
        f.write_text("---\ntitle: only meta\n---\n   ", encoding="utf-8")

        class Args:
            file = str(f)
            project = None
            force = False

        with pytest.raises(SystemExit) as exc:
            _capture_output(cmd_import_session_summary, Args())
        assert exc.value.code == 1

    def test_conversation_id_from_front_matter(self, test_db, tmp_path):
        from src.cli.commands.session import cmd_import_session_summary

        f = tmp_path / "summary.md"
        f.write_text(
            "---\nconversation_id: my-fixed-id\n---\n\nBody text", encoding="utf-8"
        )

        class Args:
            file = str(f)
            project = str(tmp_path)
            force = False

        out = _capture_output(cmd_import_session_summary, Args())
        assert "my-fixed-id" in out

    def test_project_none_defaults_to_cwd(self, test_db, tmp_path, monkeypatch):
        from src.cli.commands.session import cmd_import_session_summary

        f = tmp_path / "summary.md"
        f.write_text("Body without project arg", encoding="utf-8")
        # Point cwd at an isolated dir so the project link is deterministic.
        monkeypatch.chdir(tmp_path)

        class Args:
            file = str(f)
            project = None
            force = False

        out = _capture_output(cmd_import_session_summary, Args())
        assert "Imported session summary as conversation" in out
        assert str(tmp_path) in out


# ── build_session_review_prompt (library) ────────────────────────────

class TestBuildSessionReviewPrompt:
    def test_defaults_no_search_no_project(self, test_db):
        from src.session_review import build_session_review_prompt

        out = build_session_review_prompt(conversation_id="abcdefghijklmnop")
        assert out.startswith("# Session Retrospective — abcdefghijkl")  # 12 chars
        assert "No bugs reported." in out
        assert "No new patterns reported." in out
        assert "No workflows reported." in out
        assert "Similar Existing Entries" not in out
        assert "Engram influence" in out
        assert "Project:" not in out

    def test_project_registered(self, test_db, tmp_path):
        from src.session_review import build_session_review_prompt

        out = build_session_review_prompt(project_path=str(tmp_path))
        assert f"Project: {tmp_path.name}" in out
        assert str(tmp_path) in out

    def test_project_error_swallowed(self, test_db):
        from src.session_review import build_session_review_prompt

        with patch(
            "src.session_review.get_or_create_project",
            side_effect=RuntimeError("boom"),
        ):
            out = build_session_review_prompt(project_path="/some/path")
        assert "Project:" not in out
        assert "Session Retrospective" in out

    def test_inputs_reflected_and_similar_section(self, test_db):
        from src.session_review import build_session_review_prompt

        fake = [
            {"item_type": "mistake", "item_id": 7, "title": "Null deref"},
            {"item_type": "pattern", "item_id": 9, "title": "Race condition"},
        ]
        with patch("src.session_review.memory_search", return_value=fake) as m:
            out = build_session_review_prompt(
                bugs_fixed="segfault on load",
                new_patterns_noticed="retry storm",
                workflows_used="deploy flow",
            )
        # search called with combined query of all three terms
        args, kwargs = m.call_args
        assert "segfault on load" in args[0]
        assert kwargs["skip_audit"] is True
        assert "Bugs fixed this session: segfault on load" in out
        assert "Patterns noticed: retry storm" in out
        assert "Workflows used: deploy flow" in out
        assert "Similar Existing Entries" in out
        assert "[MISTAKE ID:7] Null deref" in out
        assert "[PATTERN ID:9] Race condition" in out

    def test_search_terms_but_no_matches(self, test_db):
        from src.session_review import build_session_review_prompt

        with patch("src.session_review.memory_search", return_value=[]):
            out = build_session_review_prompt(bugs_fixed="only a bug")
        assert "Bugs fixed this session: only a bug" in out
        assert "Similar Existing Entries" not in out
