"""Coverage tests for src/cli/commands/codebase.py.

Exercises cmd_index_project, cmd_query_codebase, cmd_clean_codebase and cmd_graph
against a real temporary DB (via the test_db fixture) and real temp project trees.
External LLM/Ollama calls are mocked at the module boundary.
"""
from __future__ import annotations

import io
import json
import os
import sys
from types import SimpleNamespace
from unittest import mock

from src.database import get_connection, get_or_create_project

# ── helpers ──────────────────────────────────────────────────────────

def _capture(func, *args, **kwargs) -> str:
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        func(*args, **kwargs)
    finally:
        sys.stdout = old
    return buf.getvalue()


def _idx_args(path, **overrides):
    base = dict(
        path=str(path), file=None, summary=None, exports=None, deps=None,
        force=False, check=False, caveman=False, caveman_level="full",
        llm_summarize=False, verbose=False,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _row(path, rel_path):
    with get_connection() as conn:
        pid = get_or_create_project(str(path))["id"]
        return conn.execute(
            "SELECT * FROM codebase_knowledge WHERE project_id = ? AND file_path = ?",
            (pid, rel_path),
        ).fetchone()


def _seed_knowledge(path, rel_path, summary="A summary", exports=None, deps=None):
    with get_connection() as conn:
        pid = get_or_create_project(str(path))["id"]
        conn.execute(
            """INSERT INTO codebase_knowledge
               (project_id, file_path, file_hash, file_mtime, summary, exports, dependencies)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (pid, rel_path, "deadbeef", 123.0, summary, exports, deps),
        )


# ── private helpers ──────────────────────────────────────────────────

class TestHelpers:
    def test_git_changed_files_returns_set_in_repo(self, tmp_path):
        import subprocess

        from src.cli.commands.codebase import _get_git_changed_files

        # Real git repo with one committed file and one untracked file.
        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=tmp_path, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
        (tmp_path / "tracked.py").write_text("x = 1\n")
        subprocess.run(["git", "add", "tracked.py"], cwd=tmp_path, check=True)
        subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp_path, check=True)
        # Modify tracked + add untracked
        (tmp_path / "tracked.py").write_text("x = 2\n")
        (tmp_path / "new.py").write_text("y = 3\n")

        changed = _get_git_changed_files(str(tmp_path))
        assert isinstance(changed, set)
        assert "tracked.py" in changed
        assert "new.py" in changed

    def test_git_changed_files_returns_none_outside_repo(self, tmp_path):
        from src.cli.commands.codebase import _get_git_changed_files

        # A non-git directory → git command fails → None.
        assert _get_git_changed_files(str(tmp_path)) is None

    def test_calculate_hash_error_on_directory(self, tmp_path):
        from src.cli.commands.codebase import _calculate_hash

        # Opening a directory as a file raises → error string returned.
        result = _calculate_hash(str(tmp_path))
        assert result.startswith("error:")

    def test_calculate_hash_matches_sha256(self, tmp_path):
        import hashlib

        from src.cli.commands.codebase import _calculate_hash

        f = tmp_path / "f.bin"
        f.write_bytes(b"hello world")
        assert _calculate_hash(str(f)) == hashlib.sha256(b"hello world").hexdigest()


# ── cmd_index_project ────────────────────────────────────────────────

class TestCmdIndexProject:
    def test_indexes_single_file_with_explicit_summary(self, test_db, tmp_path):
        from src.cli.commands.codebase import cmd_index_project

        (tmp_path / "mod.py").write_text("x = 1\n")
        out = _capture(cmd_index_project, _idx_args(tmp_path, file="mod.py", summary="Handles X"))

        assert "✓ Indexed mod.py" in out
        row = _row(tmp_path, "mod.py")
        assert row is not None
        assert row["summary"] == "Handles X"
        assert row["file_hash"] and not row["file_hash"].startswith("error:")

    def test_default_summary_placeholder_when_none(self, test_db, tmp_path):
        from src.cli.commands.codebase import cmd_index_project

        (tmp_path / "mod.py").write_text("x = 1\n")
        _capture(cmd_index_project, _idx_args(tmp_path, file="mod.py"))

        row = _row(tmp_path, "mod.py")
        assert row["summary"] == "Knowledge entry for mod.py"

    def test_stores_exports_and_deps(self, test_db, tmp_path):
        from src.cli.commands.codebase import cmd_index_project

        (tmp_path / "mod.py").write_text("x = 1\n")
        _capture(cmd_index_project, _idx_args(
            tmp_path, file="mod.py", summary="s", exports="foo,bar", deps="os,sys"))

        row = _row(tmp_path, "mod.py")
        assert row["exports"] == "foo,bar"
        assert row["dependencies"] == "os,sys"

    def test_walks_directory_indexing_only_supported_ext(self, test_db, tmp_path):
        from src.cli.commands.codebase import cmd_index_project

        (tmp_path / "a.py").write_text("x = 1\n")
        (tmp_path / "b.md").write_text("# doc\n")
        (tmp_path / "skip.txt").write_text("nope\n")
        excluded = tmp_path / "node_modules"
        excluded.mkdir()
        (excluded / "dep.py").write_text("y = 2\n")

        out = _capture(cmd_index_project, _idx_args(tmp_path))

        assert "✓ Indexed a.py" in out
        assert "✓ Indexed b.md" in out
        assert "skip.txt" not in out
        # excluded dir was pruned from the walk
        assert _row(tmp_path, "dep.py") is None
        assert _row(tmp_path, os.path.join("node_modules", "dep.py")) is None

    def test_nonexistent_file_is_skipped(self, test_db, tmp_path):
        from src.cli.commands.codebase import cmd_index_project

        out = _capture(cmd_index_project, _idx_args(tmp_path, file="ghost.py", summary="s"))
        assert out.strip() == ""
        assert _row(tmp_path, "ghost.py") is None

    def test_unchanged_mtime_match_verbose(self, test_db, tmp_path):
        from src.cli.commands.codebase import cmd_index_project

        (tmp_path / "mod.py").write_text("x = 1\n")
        _capture(cmd_index_project, _idx_args(tmp_path, file="mod.py", summary="s"))
        # Second run: same mtime → mtime-match short circuit
        out = _capture(cmd_index_project, _idx_args(tmp_path, file="mod.py", summary="s", verbose=True))
        assert "(unchanged, mtime match)" in out

    def test_unchanged_hash_match_updates_mtime(self, test_db, tmp_path):
        from src.cli.commands.codebase import cmd_index_project

        f = tmp_path / "mod.py"
        f.write_text("x = 1\n")
        _capture(cmd_index_project, _idx_args(tmp_path, file="mod.py", summary="s"))
        # Bump mtime without changing content → hash still matches
        os.utime(f, (10**9, 10**9))
        out = _capture(cmd_index_project, _idx_args(tmp_path, file="mod.py", summary="s", verbose=True))
        assert "(unchanged, hash match)" in out
        assert abs(_row(tmp_path, "mod.py")["file_mtime"] - 10**9) < 0.5

    def test_force_reindexes_and_updates_summary(self, test_db, tmp_path):
        from src.cli.commands.codebase import cmd_index_project

        (tmp_path / "mod.py").write_text("x = 1\n")
        _capture(cmd_index_project, _idx_args(tmp_path, file="mod.py", summary="old"))
        out = _capture(cmd_index_project, _idx_args(tmp_path, file="mod.py", summary="new", force=True))
        assert "✓ Indexed mod.py" in out
        assert _row(tmp_path, "mod.py")["summary"] == "new"

    def test_check_mode_emits_stale_json(self, test_db, tmp_path):
        from src.cli.commands.codebase import cmd_index_project

        (tmp_path / "mod.py").write_text("x = 1\n")
        out = _capture(cmd_index_project, _idx_args(tmp_path, file="mod.py", check=True))
        payload = json.loads(out)
        assert isinstance(payload, list)
        assert payload[0]["file_path"] == "mod.py"
        assert payload[0]["old_hash"] is None
        assert payload[0]["new_hash"]
        # check mode must NOT persist a row
        assert _row(tmp_path, "mod.py") is None

    def test_caveman_compresses_explicit_summary(self, test_db, tmp_path):
        from src.cli.commands.codebase import cmd_index_project
        from src.compression import compress_caveman

        (tmp_path / "mod.py").write_text("x = 1\n")
        summary = "This module handles the authentication of users and the storage of tokens."
        _capture(cmd_index_project, _idx_args(
            tmp_path, file="mod.py", summary=summary, caveman=True, caveman_level="full"))

        expected = compress_caveman(summary, level="full")
        assert _row(tmp_path, "mod.py")["summary"] == expected

    def test_llm_summarize_disabled_when_ollama_unavailable(self, test_db, tmp_path):
        from src.cli.commands.codebase import cmd_index_project

        (tmp_path / "mod.py").write_text("x = 1\n")
        with mock.patch("src.summarize.ollama_available", return_value=False):
            out = _capture(cmd_index_project, _idx_args(
                tmp_path, file="mod.py", summary="s", llm_summarize=True))
        assert "Ollama not available" in out

    def test_llm_summarize_uses_summarizer_result(self, test_db, tmp_path):
        from src.cli.commands.codebase import cmd_index_project

        (tmp_path / "mod.py").write_text("x = 1\n")
        fake = {"summary": "LLM says hi", "exports": "expA", "dependencies": "depB"}
        with mock.patch("src.summarize.ollama_available", return_value=True), \
             mock.patch("src.summarize.summarize_file", return_value=fake):
            out = _capture(cmd_index_project, _idx_args(
                tmp_path, file="mod.py", llm_summarize=True))

        assert "Summarizing mod.py" in out
        row = _row(tmp_path, "mod.py")
        assert row["summary"] == "LLM says hi"
        assert row["exports"] == "expA"
        assert row["dependencies"] == "depB"

    def test_reuses_existing_real_summary_when_none_provided(self, test_db, tmp_path):
        from src.cli.commands.codebase import cmd_index_project

        f = tmp_path / "mod.py"
        f.write_text("x = 1\n")
        _seed_knowledge(tmp_path, "mod.py", summary="Real human summary")
        # New content → hash differs → reindex, no summary provided → reuse existing real summary
        f.write_text("x = 2\n")
        _capture(cmd_index_project, _idx_args(tmp_path, file="mod.py"))
        assert _row(tmp_path, "mod.py")["summary"] == "Real human summary"


# ── cmd_query_codebase ───────────────────────────────────────────────

class TestCmdQueryCodebase:
    def test_empty_db_message(self, test_db, tmp_path):
        from src.cli.commands.codebase import cmd_query_codebase

        args = SimpleNamespace(path=str(tmp_path), query=[], caveman=False, caveman_level="full")
        out = _capture(cmd_query_codebase, args)
        assert "No codebase knowledge found" in out

    def test_lists_all_rows_with_fields(self, test_db, tmp_path):
        from src.cli.commands.codebase import cmd_query_codebase

        _seed_knowledge(tmp_path, "auth.py", summary="Handles auth",
                        exports="login", deps="hashlib")
        args = SimpleNamespace(path=str(tmp_path), query=[], caveman=False, caveman_level="full")
        out = _capture(cmd_query_codebase, args)

        assert "Codebase Knowledge for" in out
        assert "(1 files)" in out
        assert "auth.py" in out
        assert "Summary: Handles auth" in out
        assert "Exports:" in out and "login" in out
        assert "Deps:" in out and "hashlib" in out

    def test_query_token_filters_rows(self, test_db, tmp_path):
        from src.cli.commands.codebase import cmd_query_codebase

        _seed_knowledge(tmp_path, "auth.py", summary="Handles authentication logic")
        _seed_knowledge(tmp_path, "widgets.py", summary="Renders buttons")
        args = SimpleNamespace(path=str(tmp_path), query=["authentication"],
                               caveman=False, caveman_level="full")
        out = _capture(cmd_query_codebase, args)
        assert "auth.py" in out
        assert "widgets.py" not in out

    def test_caveman_compresses_query_output(self, test_db, tmp_path):
        from src.cli.commands.codebase import cmd_query_codebase
        from src.compression import compress_caveman

        long_summary = "This component manages the connection to the database and pooling."
        _seed_knowledge(tmp_path, "db.py", summary=long_summary)
        args = SimpleNamespace(path=str(tmp_path), query=[], caveman=True, caveman_level="full")
        out = _capture(cmd_query_codebase, args)
        assert f"Summary: {compress_caveman(long_summary, level='full')}" in out


# ── cmd_clean_codebase ───────────────────────────────────────────────

class TestCmdCleanCodebase:
    def test_already_clean_when_files_exist(self, test_db, tmp_path):
        from src.cli.commands.codebase import cmd_clean_codebase

        (tmp_path / "mod.py").write_text("x = 1\n")
        _seed_knowledge(tmp_path, "mod.py")
        out = _capture(cmd_clean_codebase, SimpleNamespace(path=str(tmp_path)))
        assert "already clean" in out
        assert _row(tmp_path, "mod.py") is not None

    def test_removes_stale_entries(self, test_db, tmp_path):
        from src.cli.commands.codebase import cmd_clean_codebase

        _seed_knowledge(tmp_path, "gone.py")
        out = _capture(cmd_clean_codebase, SimpleNamespace(path=str(tmp_path)))
        assert "Cleaned stale entry: gone.py" in out
        assert "Removed 1 stale entries" in out
        assert _row(tmp_path, "gone.py") is None


# ── cmd_graph ────────────────────────────────────────────────────────

def _make_graph_project(tmp_path):
    (tmp_path / "b.py").write_text("VALUE = 2\n")
    (tmp_path / "a.py").write_text("import b\nprint(b.VALUE)\n")


def _graph_args(path, **overrides):
    base = dict(path=str(path), file=None, direction="both",
                format="mermaid", output=None, no_index=False)
    base.update(overrides)
    return SimpleNamespace(**base)


class TestCmdGraph:
    def test_index_and_mermaid_output(self, test_db, tmp_path):
        from src.cli.commands.codebase import cmd_graph

        _make_graph_project(tmp_path)
        out = _capture(cmd_graph, _graph_args(tmp_path))
        assert "files processed" in out
        assert "```mermaid" in out
        assert "flowchart LR" in out
        assert "-->|imports|" in out

    def test_no_relationships_message(self, test_db, tmp_path):
        from src.cli.commands.codebase import cmd_graph

        # empty project, skip indexing → nothing in DB
        out = _capture(cmd_graph, _graph_args(tmp_path, no_index=True))
        assert "No relationships found" in out

    def test_dot_format(self, test_db, tmp_path):
        from src.cli.commands.codebase import cmd_graph

        _make_graph_project(tmp_path)
        out = _capture(cmd_graph, _graph_args(tmp_path, format="dot"))
        assert "digraph engram_codebase" in out
        assert '"a.py" -> "b.py"' in out

    def test_json_format(self, test_db, tmp_path):
        from src.cli.commands.codebase import cmd_graph

        _make_graph_project(tmp_path)
        out = _capture(cmd_graph, _graph_args(tmp_path, format="json"))
        # strip the leading indexing status lines, then parse the JSON body
        body = out[out.index("{"):]
        data = json.loads(body)
        assert "a.py" in data["nodes"] and "b.py" in data["nodes"]
        assert any(e["source_file"] == "a.py" and e["target_file"] == "b.py"
                   for e in data["edges"])

    def test_writes_output_file(self, test_db, tmp_path):
        from src.cli.commands.codebase import cmd_graph

        _make_graph_project(tmp_path)
        out_file = tmp_path / "graph.mmd"
        out = _capture(cmd_graph, _graph_args(tmp_path, output=str(out_file)))
        assert f"Graph written to {out_file}" in out
        assert "flowchart LR" in out_file.read_text()

    def test_direction_incoming_filter(self, test_db, tmp_path):
        from src.cli.commands.codebase import cmd_graph

        _make_graph_project(tmp_path)
        # index first
        _capture(cmd_graph, _graph_args(tmp_path))
        # b.py is imported-by a.py → incoming relationships exist for b.py
        out = _capture(cmd_graph, _graph_args(
            tmp_path, no_index=True, file="b.py", direction="incoming"))
        assert "-->|imports|" in out
        # a.py has no incoming edges → empty
        out2 = _capture(cmd_graph, _graph_args(
            tmp_path, no_index=True, file="a.py", direction="incoming"))
        assert "No relationships found" in out2
