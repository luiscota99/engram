"""Coverage tests for src/cli/commands/memory.py.

Every test drives a cmd_* function with a fake args object and asserts on the
captured stdout, DB side effects, or the raised SystemExit — matching the idiom
in tests/test_cli_commands.py. External I/O (dedup scan, semantic search,
consolidation scan, stats) is mocked at the module boundary where needed.
"""
from __future__ import annotations

import io
import json
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from src.database import get_connection
from src.memory_ops import (
    create_conversation,
    create_prompt,
    create_session,
    create_skill,
)


def _capture(func, *args, **kwargs) -> str:
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        func(*args, **kwargs)
    finally:
        sys.stdout = old
    return buf.getvalue()


# ── _cli_dedup_gate ──────────────────────────────────────────────────

class TestDedupGate:
    def test_force_bypasses_check(self, test_db):
        from src.cli.commands.memory import _cli_dedup_gate

        args = SimpleNamespace(force=True)
        # Even if the checker would flag a dup, force short-circuits before it.
        with patch("src.cli.commands.memory.check_duplicate_before_add") as chk:
            assert _cli_dedup_gate(args, "content", "mistake") is True
            chk.assert_not_called()

    def test_no_duplicates_allows(self, test_db):
        from src.cli.commands.memory import _cli_dedup_gate

        args = SimpleNamespace(force=False)
        with patch(
            "src.cli.commands.memory.check_duplicate_before_add",
            return_value={"duplicates": []},
        ):
            assert _cli_dedup_gate(args, "content", "mistake") is True

    def test_duplicate_blocks_and_prints_details(self, test_db):
        from src.cli.commands.memory import _cli_dedup_gate

        args = SimpleNamespace(force=False)
        dup = {
            "duplicates": [
                {
                    "item_type": "skill",
                    "item_id": 7,
                    "title": "Deploy Flow",
                    "similarity": 0.95,
                    "match_kind": "semantic",
                }
            ]
        }
        buf = io.StringIO()
        old = sys.stdout
        sys.stdout = buf
        try:
            with patch(
                "src.cli.commands.memory.check_duplicate_before_add",
                return_value=dup,
            ):
                result = _cli_dedup_gate(args, "content", "skill", name="Deploy Flow")
        finally:
            sys.stdout = old
        out = buf.getvalue()
        assert result is False
        assert "insert blocked" in out
        assert "[SKILL ID:7]" in out
        assert "Deploy Flow" in out
        assert "0.95" in out
        assert "semantic" in out


# ── cmd_add dispatch ─────────────────────────────────────────────────

class TestCmdAddDispatch:
    def test_unknown_kind_exits(self, test_db):
        from src.cli.commands.memory import cmd_add

        args = SimpleNamespace(kind="bogus")
        with pytest.raises(SystemExit) as exc:
            _capture(cmd_add, args)
        assert exc.value.code == 1

    def test_dispatches_to_mistake(self, test_db):
        from src.cli.commands.memory import cmd_add

        args = SimpleNamespace(
            kind="mistake",
            force=True,
            date="2026-07-13",
            context="ctx",
            mistake="broke build",
            root_cause="typo",
            fix="fixed typo",
            prevention="lint",
            conversation=None,
            tags=None,
        )
        out = _capture(cmd_add, args)
        assert "Mistake #" in out
        with get_connection(test_db["path"]) as conn:
            row = conn.execute("SELECT mistake FROM mistakes").fetchone()
        assert row["mistake"] == "broke build"


# ── _add_mistake ─────────────────────────────────────────────────────

class TestAddMistake:
    def _args(self, **over):
        base = dict(
            force=True,
            date="2026-07-13",
            context="ctx",
            mistake="the mistake",
            root_cause="rc",
            fix="the fix",
            prevention="prev",
            conversation=None,
            tags="a,b",
        )
        base.update(over)
        return SimpleNamespace(**base)

    def test_inserts_and_reports_id(self, test_db):
        from src.cli.commands.memory import _add_mistake

        out = _capture(_add_mistake, self._args())
        assert "Mistake #1 logged" in out
        with get_connection(test_db["path"]) as conn:
            row = conn.execute("SELECT mistake, fix FROM mistakes WHERE id=1").fetchone()
        assert row["mistake"] == "the mistake"
        assert row["fix"] == "the fix"

    def test_blocked_by_dedup_exits(self, test_db):
        from src.cli.commands.memory import _add_mistake

        dup = {"duplicates": [{"item_type": "mistake", "item_id": 3, "title": "x"}]}
        with patch(
            "src.cli.commands.memory.check_duplicate_before_add", return_value=dup
        ):
            with pytest.raises(SystemExit):
                _capture(_add_mistake, self._args(force=False))
        with get_connection(test_db["path"]) as conn:
            n = conn.execute("SELECT COUNT(*) c FROM mistakes").fetchone()["c"]
        assert n == 0


# ── _add_pattern / _add_skill ────────────────────────────────────────

class TestAddPatternSkill:
    def test_add_pattern(self, test_db):
        from src.cli.commands.memory import _add_pattern

        args = SimpleNamespace(
            force=True,
            name="Flaky Test",
            symptoms="random failures",
            root_cause="timing",
            fix="add retry",
            tags="ci",
        )
        out = _capture(_add_pattern, args)
        assert "Pattern #1 'Flaky Test' logged" in out
        with get_connection(test_db["path"]) as conn:
            row = conn.execute("SELECT name, symptoms FROM patterns").fetchone()
        assert row["name"] == "Flaky Test"
        assert row["symptoms"] == "random failures"

    def test_add_skill(self, test_db):
        from src.cli.commands.memory import _add_skill

        args = SimpleNamespace(
            force=True,
            name="Deploy",
            domain="devops",
            trigger="on release",
            workflow="step1\nstep2",
            pitfalls="watch env",
            files="deploy.sh",
            dependencies="docker",
            tags="ops",
        )
        out = _capture(_add_skill, args)
        assert "Skill #1 'Deploy' logged" in out
        with get_connection(test_db["path"]) as conn:
            row = conn.execute("SELECT name, domain FROM skills").fetchone()
        assert row["name"] == "Deploy"
        assert row["domain"] == "devops"


# ── _add_conversation / _add_prompt ──────────────────────────────────

class TestAddConversationPrompt:
    def test_add_conversation(self, test_db):
        from src.cli.commands.memory import _add_conversation

        args = SimpleNamespace(
            id="conv-123",
            title="Big Refactor",
            date="2026-07-13",
            domain="eng",
            tasks="did stuff",
            decisions="chose X",
            mistakes="none",
            skills="none",
            tags="refactor",
        )
        out = _capture(_add_conversation, args)
        assert "Conversation #1 'Big Refactor' logged" in out
        with get_connection(test_db["path"]) as conn:
            row = conn.execute("SELECT conversation_id, title FROM conversations").fetchone()
        assert row["conversation_id"] == "conv-123"
        assert row["title"] == "Big Refactor"

    def test_add_prompt_inline_text(self, test_db):
        from src.cli.commands.memory import _add_prompt

        args = SimpleNamespace(
            name="Reviewer",
            role="critic",
            domain="eng",
            description="reviews code",
            prompt_text="Be harsh.",
            file=None,
            best_for="PRs",
            tags="review",
        )
        out = _capture(_add_prompt, args)
        assert "Prompt #1 'Reviewer' stored" in out
        with get_connection(test_db["path"]) as conn:
            row = conn.execute("SELECT name, prompt_text FROM prompts").fetchone()
        assert row["name"] == "Reviewer"
        assert row["prompt_text"] == "Be harsh."

    def test_add_prompt_reads_from_file(self, test_db, tmp_path):
        from src.cli.commands.memory import _add_prompt

        pf = tmp_path / "p.txt"
        pf.write_text("From file content.", encoding="utf-8")
        args = SimpleNamespace(
            name="FilePrompt",
            role="worker",
            domain="eng",
            description="from file",
            prompt_text=None,
            file=str(pf),
            best_for=None,
            tags=None,
        )
        out = _capture(_add_prompt, args)
        assert "Prompt #1 'FilePrompt' stored" in out
        with get_connection(test_db["path"]) as conn:
            row = conn.execute("SELECT prompt_text, source_path FROM prompts").fetchone()
        assert row["prompt_text"] == "From file content."
        assert row["source_path"] == str(pf)


# ── _add_session / _add_transcript ───────────────────────────────────

class TestAddSessionTranscript:
    def _session_args(self, sid="sess-1"):
        return SimpleNamespace(
            id=sid,
            title="Session One",
            date="2026-07-13",
            domain="eng",
            workflow_used=None,
        )

    def test_add_session_initializes(self, test_db):
        from src.cli.commands.memory import _add_session

        out = _capture(_add_session, self._session_args())
        assert "Session 'sess-1' initialized" in out
        with get_connection(test_db["path"]) as conn:
            row = conn.execute("SELECT title FROM sessions WHERE session_id='sess-1'").fetchone()
            state = conn.execute(
                "SELECT current_phase FROM session_state WHERE session_id='sess-1'"
            ).fetchone()
        assert row["title"] == "Session One"
        assert state is not None

    def test_add_transcript(self, test_db):
        from src.cli.commands.memory import _add_session, _add_transcript

        _capture(_add_session, self._session_args("sess-2"))
        args = SimpleNamespace(
            session_id="sess-2", role="analyst", content="my analysis"
        )
        out = _capture(_add_transcript, args)
        assert "Transcript entry for 'analyst' added to session 'sess-2'" in out
        with get_connection(test_db["path"]) as conn:
            row = conn.execute(
                "SELECT role, content FROM session_transcripts WHERE session_id='sess-2'"
            ).fetchone()
        assert row["role"] == "analyst"
        assert row["content"] == "my analysis"


# ── cmd_add_decision ─────────────────────────────────────────────────

class TestCmdAddDecision:
    def test_allowed_when_no_state(self, test_db):
        from src.cli.commands.memory import cmd_add_decision

        # Session created directly => no session_state row => gate allows.
        with get_connection(test_db["path"]) as conn:
            create_session(
                conn,
                session_id="raw-sess",
                title="Raw",
                date="2026-07-13",
                domain="eng",
            )
        args = SimpleNamespace(
            force_bypass=False, session_id="raw-sess", decision="ship it"
        )
        out = _capture(cmd_add_decision, args)
        assert "Decision added to session 'raw-sess'" in out
        with get_connection(test_db["path"]) as conn:
            row = conn.execute(
                "SELECT key_decisions FROM sessions WHERE session_id='raw-sess'"
            ).fetchone()
        assert "ship it" in row["key_decisions"]

    def test_workflow_violation_exits(self, test_db):
        from src.cli.commands.memory import _add_session, cmd_add_decision

        # init_session_state creates a phase with required roles not yet met.
        _capture(
            _add_session,
            SimpleNamespace(
                id="gated",
                title="Gated",
                date="2026-07-13",
                domain="eng",
                workflow_used=None,
            ),
        )
        args = SimpleNamespace(
            force_bypass=False, session_id="gated", decision="premature"
        )
        buf = io.StringIO()
        old = sys.stdout
        sys.stdout = buf
        try:
            with pytest.raises(SystemExit):
                cmd_add_decision(args)
        finally:
            sys.stdout = old
        assert "WorkflowViolation" in buf.getvalue()

    def test_force_bypass_skips_gate(self, test_db):
        from src.cli.commands.memory import _add_session, cmd_add_decision

        _capture(
            _add_session,
            SimpleNamespace(
                id="bypass",
                title="Bypass",
                date="2026-07-13",
                domain="eng",
                workflow_used=None,
            ),
        )
        args = SimpleNamespace(
            force_bypass=True, session_id="bypass", decision="override"
        )
        out = _capture(cmd_add_decision, args)
        assert "Decision added to session 'bypass'" in out


# ── cmd_search ───────────────────────────────────────────────────────

class TestCmdSearch:
    def _seed(self, path):
        with get_connection(path) as conn:
            create_skill(
                conn,
                name="Kafka Retry Skill",
                domain="eng",
                trigger="when consumer stalls",
                workflow="restart\nbackoff",
                tags="kafka,retry",
            )

    def test_result_shows_title_and_tags(self, test_db):
        from src.cli.commands.memory import cmd_search

        self._seed(test_db["path"])
        args = SimpleNamespace(
            query=["Kafka"],
            type=None,
            tags=None,
            limit=10,
            project=None,
            no_project=True,
        )
        out = _capture(cmd_search, args)
        assert "Found 1 result(s)" in out
        assert "Kafka Retry Skill" in out
        assert "tags:" in out

    def test_project_path_branch(self, test_db, tmp_path):
        from src.cli.commands.memory import cmd_search

        args = SimpleNamespace(
            query=["nothing"],
            type=None,
            tags="kafka",
            limit=10,
            project=str(tmp_path),
            no_project=False,
        )
        out = _capture(cmd_search, args)
        assert "No results found" in out

    def test_default_cwd_project_branch(self, test_db):
        from src.cli.commands.memory import cmd_search

        # project=None and no_project=False => project_path defaults to cwd.
        args = SimpleNamespace(
            query=["nothing-here-xyz"],
            type=None,
            tags=None,
            limit=10,
            project=None,
            no_project=False,
        )
        out = _capture(cmd_search, args)
        assert "No results found" in out


# ── cmd_recent ───────────────────────────────────────────────────────

class TestCmdRecent:
    def test_empty_db(self, test_db):
        from src.cli.commands.memory import cmd_recent

        out = _capture(cmd_recent, SimpleNamespace(n=10, type=None))
        assert "No entries yet" in out

    def test_shows_tags_line(self, test_db):
        from src.cli.commands.memory import cmd_recent

        with get_connection(test_db["path"]) as conn:
            create_skill(
                conn,
                name="Tagged Skill",
                domain="eng",
                trigger="t",
                workflow="w",
                tags="alpha",
            )
        args = SimpleNamespace(n=10, type=None)
        out = _capture(cmd_recent, args)
        assert "Tagged Skill" in out
        assert "alpha" in out


# ── cmd_list (conversations, sessions, prompts, unknown) ─────────────

class TestCmdListMore:
    def test_list_skills(self, test_db):
        from src.cli.commands.memory import cmd_list

        with get_connection(test_db["path"]) as conn:
            create_skill(
                conn,
                name="Listed Skill",
                domain="devops",
                trigger="on deploy",
                workflow="w",
                tags="ops",
            )
        out = _capture(cmd_list, SimpleNamespace(kind="skills"))
        assert "Skills (1)" in out
        assert "Listed Skill" in out
        assert "devops" in out
        assert "ops" in out

    def test_list_conversations(self, test_db):
        from src.cli.commands.memory import cmd_list

        with get_connection(test_db["path"]) as conn:
            create_conversation(
                conn,
                conversation_id="c-abcdef123456xyz",
                title="Convo Title",
                date="2026-07-13",
                domain="eng",
                tags="talk",
            )
        out = _capture(cmd_list, SimpleNamespace(kind="conversations"))
        assert "Conversations (1)" in out
        assert "Convo Title" in out
        assert "talk" in out

    def test_list_sessions(self, test_db):
        from src.cli.commands.memory import cmd_list

        with get_connection(test_db["path"]) as conn:
            create_session(
                conn,
                session_id="sess-list-1",
                title="Sess Title",
                date="2026-07-13",
                domain="eng",
                workflow_used="committee",
            )
        out = _capture(cmd_list, SimpleNamespace(kind="sessions"))
        assert "Sessions (1)" in out
        assert "Sess Title" in out
        assert "committee" in out

    def test_list_prompts(self, test_db):
        from src.cli.commands.memory import cmd_list

        with get_connection(test_db["path"]) as conn:
            create_prompt(
                conn,
                name="P One",
                role="critic",
                domain="eng",
                description="d",
                best_for="reviews",
                tags="pr",
            )
        out = _capture(cmd_list, SimpleNamespace(kind="prompts"))
        assert "Prompts (1)" in out
        assert "P One" in out
        assert "Best for:" in out
        assert "pr" in out

    def test_list_patterns_with_occurrence(self, test_db):
        from src.cli.commands.memory import _add_pattern, cmd_link_pattern, cmd_list

        _capture(
            _add_pattern,
            SimpleNamespace(
                force=True,
                name="Race",
                symptoms="s",
                root_cause="rc",
                fix="f",
                tags="con",
            ),
        )
        _capture(
            cmd_link_pattern,
            SimpleNamespace(
                name="Race", conversation="c1", date="2026-07-13", notes="seen again"
            ),
        )
        out = _capture(cmd_list, SimpleNamespace(kind="patterns"))
        assert "Race" in out
        assert "1 occurrence" in out

    def test_list_mistakes_with_entry(self, test_db):
        from src.cli.commands.memory import _add_mistake, cmd_list

        _capture(
            _add_mistake,
            SimpleNamespace(
                force=True,
                date="2026-07-13",
                context="c",
                mistake="oops",
                root_cause="rc",
                fix="patched",
                prevention="p",
                conversation=None,
                tags="bug",
            ),
        )
        out = _capture(cmd_list, SimpleNamespace(kind="mistakes"))
        assert "oops" in out
        assert "patched" in out
        assert "bug" in out

    def test_unknown_kind_exits(self, test_db):
        from src.cli.commands.memory import cmd_list

        with pytest.raises(SystemExit):
            _capture(cmd_list, SimpleNamespace(kind="bogus"))


# ── cmd_suggest ──────────────────────────────────────────────────────

class TestCmdSuggest:
    def test_no_matches(self, test_db):
        from src.cli.commands.memory import cmd_suggest

        args = SimpleNamespace(query=["zzz"], type="prompt", limit=3)
        out = _capture(cmd_suggest, args)
        assert "No matching prompts found" in out

    def test_lexical_match(self, test_db):
        from src.cli.commands.memory import cmd_suggest

        with get_connection(test_db["path"]) as conn:
            create_prompt(
                conn,
                name="Debug Helper",
                role="debugger",
                domain="eng",
                description="helps debug crashes",
                tags="debug",
            )
        # 2-word query => lexical branch (no semantic).
        args = SimpleNamespace(query=["debug"], type="prompt", limit=3)
        out = _capture(cmd_suggest, args)
        assert "Lexical" in out
        assert "Debug Helper" in out

    def test_semantic_branch(self, test_db):
        from src.cli.commands.memory import cmd_suggest

        sem = [
            {
                "item_type": "prompt",
                "title": "Semantic Prompt",
                "snippet": "line one\nline two",
                "tags": "sem",
            }
        ]
        # >2 words triggers semantic_search; mock it to return a prompt hit.
        with patch(
            "src.cli.commands.memory.semantic_search",
            return_value=(sem, None),
        ):
            args = SimpleNamespace(
                query=["find", "me", "a", "prompt"], type="prompt", limit=3
            )
            out = _capture(cmd_suggest, args)
        assert "Semantic" in out
        assert "Semantic Prompt" in out
        assert "line one line two" in out
        assert "sem" in out


# ── cmd_stats (embeddings branch) ────────────────────────────────────

class TestCmdStatsEmbeddings:
    def test_embedding_breakdown(self, test_db):
        from src.cli.commands.memory import cmd_stats

        rich = {
            "mistakes": 1,
            "patterns": 2,
            "skills": 3,
            "conversations": 4,
            "prompts": 5,
            "tags": 6,
            "fts_indexed": 7,
            "embeddings": {
                "total": 10,
                "model": "nomic-embed",
                "ready": 6,
                "stale": 2,
                "pending": 1,
                "failed": 1,
            },
        }
        with patch("src.cli.commands.memory.get_stats", return_value=rich):
            out = _capture(cmd_stats, SimpleNamespace())
        assert "nomic-embed" in out
        assert "Ready:" in out
        assert "Stale:" in out
        assert "Pending:" in out
        assert "Failed:" in out
        assert "reembed" in out

    def test_no_embeddings_tracked(self, test_db):
        from src.cli.commands.memory import cmd_stats

        rich = {
            "mistakes": 0,
            "patterns": 0,
            "skills": 0,
            "conversations": 0,
            "prompts": 0,
            "tags": 0,
            "fts_indexed": 0,
            "embeddings": {"total": 0, "model": "none"},
        }
        with patch("src.cli.commands.memory.get_stats", return_value=rich):
            out = _capture(cmd_stats, SimpleNamespace())
        assert "No embeddings tracked yet" in out


# ── cmd_link_pattern ─────────────────────────────────────────────────

class TestCmdLinkPattern:
    def test_links_to_existing_pattern(self, test_db):
        from src.cli.commands.memory import _add_pattern, cmd_link_pattern

        _capture(
            _add_pattern,
            SimpleNamespace(
                force=True,
                name="Deadlock",
                symptoms="s",
                root_cause="rc",
                fix="f",
                tags=None,
            ),
        )
        out = _capture(
            cmd_link_pattern,
            SimpleNamespace(
                name="Deadlock",
                conversation="conv-1",
                date="2026-07-13",
                notes="observed",
            ),
        )
        assert "Linked pattern 'Deadlock'" in out
        with get_connection(test_db["path"]) as conn:
            n = conn.execute("SELECT COUNT(*) c FROM pattern_occurrences").fetchone()["c"]
        assert n == 1

    def test_missing_pattern_exits(self, test_db):
        from src.cli.commands.memory import cmd_link_pattern

        buf = io.StringIO()
        old = sys.stdout
        sys.stdout = buf
        try:
            with pytest.raises(SystemExit):
                cmd_link_pattern(
                    SimpleNamespace(
                        name="Ghost",
                        conversation="c",
                        date=None,
                        notes=None,
                    )
                )
        finally:
            sys.stdout = old
        assert "Pattern 'Ghost' not found" in buf.getvalue()


# ── cmd_consolidate ──────────────────────────────────────────────────

class TestCmdConsolidate:
    def test_consolidates_and_deletes(self, test_db):
        from src.cli.commands.memory import cmd_consolidate

        with get_connection(test_db["path"]) as conn:
            a = create_skill(
                conn, name="Old A", domain="eng", trigger="t", workflow="w"
            )
            b = create_skill(
                conn, name="Old B", domain="eng", trigger="t", workflow="w"
            )
        args = SimpleNamespace(
            delete_ids=f"{a},{b}",
            name="Master Skill",
            domain="eng",
            trigger="combined trigger",
            workflow="combined wf",
            pitfalls="p",
            key_files="f.py",
            deps="dep",
            tags="merged",
        )
        out = _capture(cmd_consolidate, args)
        assert "Consolidated 2 skills" in out
        with get_connection(test_db["path"]) as conn:
            names = [
                r["name"] for r in conn.execute("SELECT name FROM skills").fetchall()
            ]
        assert "Master Skill" in names
        assert "Old A" not in names
        assert "Old B" not in names

    def test_empty_ids_exits(self, test_db):
        from src.cli.commands.memory import cmd_consolidate

        buf = io.StringIO()
        old = sys.stdout
        sys.stdout = buf
        try:
            with pytest.raises(SystemExit):
                cmd_consolidate(
                    SimpleNamespace(
                        delete_ids="  ,  ",
                        name="x",
                        domain="x",
                        trigger="x",
                        workflow="x",
                        pitfalls=None,
                        key_files=None,
                        deps=None,
                        tags=None,
                    )
                )
        finally:
            sys.stdout = old
        assert "requires at least one ID" in buf.getvalue()


# ── cmd_suggest_consolidate ──────────────────────────────────────────

class TestCmdSuggestConsolidate:
    def test_prints_multi_item_cluster(self, test_db):
        from src.cli.commands.memory import cmd_suggest_consolidate

        cluster = {
            "item_type": "skill",
            "cluster_size": 3,
            "avg_similarity": 0.88,
            "items": [
                {"item_id": 10, "title": "S10"},
                {"item_id": 11, "title": "S11"},
                {"item_id": 12, "title": "S12"},
            ],
        }
        with patch(
            "src.cli.commands.memory.find_consolidation_candidates",
            return_value=([cluster], None),
        ):
            out = _capture(
                cmd_suggest_consolidate,
                SimpleNamespace(threshold=0.8, type="skill", limit=10),
            )
        assert "3 items" in out
        assert "--delete-ids 10,11,12" in out
        assert "covering 3 items" in out

    def test_no_candidates(self, test_db):
        from src.cli.commands.memory import cmd_suggest_consolidate

        with patch(
            "src.cli.commands.memory.find_consolidation_candidates",
            return_value=([], None),
        ):
            out = _capture(
                cmd_suggest_consolidate,
                SimpleNamespace(threshold=0.8, type=None, limit=10),
            )
        assert "No consolidation candidates" in out


# ── cmd_suggest_capture ──────────────────────────────────────────────

class TestCmdSuggestCapture:
    def test_json_output(self, test_db):
        from src.cli.commands.memory import cmd_suggest_capture

        args = SimpleNamespace(
            task="Fixed a flaky migration",
            outcome="Added an explicit transaction wrapper",
            errors="OperationalError: database is locked",
            files="db.py",
            json=True,
        )
        out = _capture(cmd_suggest_capture, args)
        data = json.loads(out)
        assert "suggested_types" in data
        assert "domain" in data

    def test_text_output(self, test_db):
        from src.cli.commands.memory import cmd_suggest_capture

        args = SimpleNamespace(
            task="Wrote the deploy workflow",
            outcome="Deployed successfully to prod",
            errors=None,
            files=None,
            json=False,
        )
        out = _capture(cmd_suggest_capture, args)
        assert "Engram Memory Capture Suggestion" in out


# ── cmd_session_help ─────────────────────────────────────────────────

class TestCmdSessionHelp:
    def test_writes_line_and_truncates(self, tmp_path, monkeypatch):
        from src.cli.commands.memory import cmd_session_help

        log = tmp_path / "sub" / "help.jsonl"
        monkeypatch.setenv("ENGRAM_SESSION_HELP_LOG", str(log))
        args = SimpleNamespace(
            score=3, note="n" * 3000, task="t" * 800
        )
        out = _capture(cmd_session_help, args)
        assert "Logged Session Help Score 3" in out
        line = json.loads(log.read_text().strip().splitlines()[-1])
        assert line["score"] == 3
        assert len(line["note"]) == 2000
        assert len(line["task"]) == 500

    def test_rejects_out_of_range(self, tmp_path, monkeypatch):
        from src.cli.commands.memory import cmd_session_help

        monkeypatch.setenv("ENGRAM_SESSION_HELP_LOG", str(tmp_path / "h.jsonl"))
        with pytest.raises(SystemExit):
            cmd_session_help(SimpleNamespace(score=-1, note=None, task=None))

    def test_write_error_exits(self, tmp_path, monkeypatch):
        from src.cli.commands.memory import cmd_session_help

        # Point the log at a path whose parent is a file => open() raises OSError.
        blocker = tmp_path / "blocker"
        blocker.write_text("x", encoding="utf-8")
        monkeypatch.setenv("ENGRAM_SESSION_HELP_LOG", str(blocker / "nested.jsonl"))
        with pytest.raises(SystemExit):
            cmd_session_help(SimpleNamespace(score=1, note=None, task=None))
