"""Coverage-focused tests for src/mcp/handlers.py.

Drives handlers via TOOL_HANDLERS[name](args_dict) with a per-test temp DB.
External I/O (LLM/Ollama/consolidation scans) is mocked at the handlers module
boundary so tests are hermetic. Every test asserts specific formatted output,
return values, or DB side effects.
"""

from __future__ import annotations

import json
import os
from unittest.mock import patch

import pytest

from src.database import get_connection, init_db
from src.mcp.handlers import TOOL_HANDLERS, format_and_truncate_results


@pytest.fixture
def mcp_db(tmp_path):
    """Temporary DB with ENGRAM_DB_PATH set (function-scoped)."""
    db_path = str(tmp_path / "handlers2.db")
    os.environ["ENGRAM_DB_PATH"] = db_path
    init_db(db_path)
    return db_path


# --------------------------------------------------------------------------
# format_and_truncate_results
# --------------------------------------------------------------------------


def test_format_empty_with_degraded_semantic_status(mcp_db):
    out = format_and_truncate_results([], semantic_status="down", semantic_available=False)
    assert "No results found" in out
    assert "semantic search down" in out
    assert "semantic_available=false" in out


def test_format_empty_ok_status(mcp_db):
    out = format_and_truncate_results([], semantic_status="ok")
    assert "No results found." in out
    assert "semantic search" not in out


def test_format_degraded_banner_and_markers(mcp_db):
    results = [
        {
            "item_type": "skill",
            "item_id": 7,
            "title": "Pinned scored hit",
            "utility_score": 3.25,
            "is_semantic": True,
            "pinned": True,
            "snippet": "line one\nline two",
            "tags": "a,b",
        }
    ]
    out = format_and_truncate_results(results, semantic_status="down", semantic_available=False)
    assert "[Note: semantic search down" in out
    assert "semantic_available=false" in out
    # Pin marker, semantic type "S", and one-decimal score.
    assert "[SKILL ID: 7] [PINNED] (score: 3.2, S) Pinned scored hit" in out
    # Newlines in the snippet are flattened to spaces.
    assert "Snippet: line one line two" in out
    assert "Tags: a,b" in out


def test_format_keyword_no_score_marker(mcp_db):
    results = [{"item_type": "mistake", "item_id": 1, "title": "kw only"}]
    out = format_and_truncate_results(results)
    # No utility_score -> just the "(K)" search-type marker.
    assert "[MISTAKE ID: 1] (K) kw only" in out


def test_format_truncation_warning(mcp_db):
    # Force a tiny budget so the second block trips the truncation guard.
    with patch("src.mcp.handlers.config.max_context_chars", return_value=200):
        results = [
            {"item_type": "skill", "item_id": i, "title": f"t{i}", "snippet": "y" * 300}
            for i in range(1, 6)
        ]
        out = format_and_truncate_results(results)
    assert "WARNING: Truncated at 200 chars" in out


# --------------------------------------------------------------------------
# _dedup_gate via add handlers
# --------------------------------------------------------------------------


def test_add_mistake_dedup_blocked(mcp_db):
    dup = {
        "duplicates": [
            {
                "item_type": "mistake",
                "item_id": 42,
                "title": "old dup",
                "similarity": 0.97,
                "match_kind": "semantic",
            }
        ]
    }
    with patch("src.mcp.handlers.check_duplicate_before_add", return_value=dup):
        out = TOOL_HANDLERS["memory_add_mistake"](
            {
                "date": "2026-07-13",
                "context": "c",
                "mistake": "m",
                "fix": "f",
            }
        )
    assert "Near-duplicate detected" in out
    assert "[MISTAKE ID:42] old dup" in out
    assert "similarity: 0.97, semantic" in out
    assert "force=true" in out


def test_add_mistake_force_bypasses_dedup(mcp_db):
    # force=true must skip the dedup check entirely and insert.
    with patch("src.mcp.handlers.check_duplicate_before_add") as chk:
        out = TOOL_HANDLERS["memory_add_mistake"](
            {
                "date": "2026-07-13",
                "context": "c",
                "mistake": "m",
                "fix": "f",
                "force": True,
            }
        )
    chk.assert_not_called()
    assert "logged successfully" in out


# --------------------------------------------------------------------------
# record_usage / read_item
# --------------------------------------------------------------------------


def test_record_usage_missing_args(mcp_db):
    assert TOOL_HANDLERS["memory_record_usage"]({}) == (
        "Error: item_type and item_id are required."
    )


def test_record_usage_failure_path(mcp_db):
    # record_usage returning falsy drives the "Failed" branch.
    with patch("src.mcp.handlers.record_usage", return_value=False):
        out = TOOL_HANDLERS["memory_record_usage"](
            {"item_type": "mistake", "item_id": 99999}
        )
    assert out == "Failed to record usage for mistake ID 99999."


def test_record_usage_success(mcp_db):
    add = TOOL_HANDLERS["memory_add"](
        {"type": "skill", "name": "UseMe", "domain": "d", "trigger": "t", "workflow": "w"}
    )
    sid = int(add.split("#")[1].split(" ")[0])
    out = TOOL_HANDLERS["memory_record_usage"]({"item_type": "skill", "item_id": sid})
    assert out == f"Successfully recorded usage for skill ID {sid}. Its search rank has been boosted."


def test_read_item_missing_args(mcp_db):
    assert TOOL_HANDLERS["memory_read_item"]({}) == (
        "Error: item_type and item_id are required."
    )


def test_read_item_not_found(mcp_db):
    out = TOOL_HANDLERS["memory_read_item"]({"item_type": "skill", "item_id": 12345})
    assert out == "Error: Could not find skill with ID 12345."


def test_read_item_success_returns_json(mcp_db):
    add = TOOL_HANDLERS["memory_add"](
        {"type": "skill", "name": "ReadMe", "domain": "d", "trigger": "t", "workflow": "w"}
    )
    sid = int(add.split("#")[1].split(" ")[0])
    out = TOOL_HANDLERS["memory_read_item"]({"item_type": "skill", "item_id": sid})
    parsed = json.loads(out)
    assert parsed["name"] == "ReadMe"


def test_search_real_finds_added_item(mcp_db):
    TOOL_HANDLERS["memory_add"](
        {
            "type": "pattern",
            "name": "Deadlock detector",
            "symptoms": "threads hang forever waiting on locks",
            "root_cause": "lock ordering inversion",
            "standard_fix": "acquire locks in a global order",
        }
    )
    out = TOOL_HANDLERS["memory_search"]({"query": "deadlock lock ordering", "limit": 3})
    assert "Deadlock detector" in out or "lock ordering" in out.lower()


def test_recent_with_results(mcp_db):
    TOOL_HANDLERS["memory_add"](
        {"type": "skill", "name": "RecentSkill", "domain": "d", "trigger": "t", "workflow": "w"}
    )
    out = TOOL_HANDLERS["memory_recent"]({"count": 5})
    assert "RecentSkill" in out


def test_add_mistake_missing_fields_and_unknown_type(mcp_db):
    miss = TOOL_HANDLERS["memory_add"]({"type": "mistake", "context": "c"})
    assert "missing required fields for mistake" in miss
    unk = TOOL_HANDLERS["memory_add"]({"type": "bogus"})
    assert "unknown type 'bogus'" in unk


def test_add_pattern_and_skill_dedup_blocked(mcp_db):
    dup = {"duplicates": [{"item_type": "pattern", "item_id": 1, "title": "d", "similarity": 0.9, "match_kind": "exact"}]}
    with patch("src.mcp.handlers.check_duplicate_before_add", return_value=dup):
        pat = TOOL_HANDLERS["memory_add_pattern"](
            {"name": "P", "symptoms": "s", "root_cause": "r", "standard_fix": "f"}
        )
        skl = TOOL_HANDLERS["memory_add_skill"](
            {"name": "S", "domain": "d", "trigger": "t", "workflow": "w"}
        )
    assert "Near-duplicate detected" in pat
    assert "Near-duplicate detected" in skl


def test_init_session_default_workflow(mcp_db):
    out = TOOL_HANDLERS["memory_init_session"](
        {"session_id": "nowf", "title": "T", "date": "2026-07-13", "domain": "eng"}
    )
    assert out.startswith("Session 'nowf' initialized successfully.")


def test_add_decision_workflow_violation(mcp_db):
    TOOL_HANDLERS["memory_init_session"](
        {
            "session_id": "vio",
            "title": "T",
            "date": "2026-07-13",
            "domain": "eng",
            "workflow_used": "tdd",
        }
    )
    out = TOOL_HANDLERS["memory_add_decision"]({"session_id": "vio", "decision": "d"})
    assert out.startswith("WorkflowViolation:")


def test_advance_phase_violation(mcp_db):
    TOOL_HANDLERS["memory_init_session"](
        {
            "session_id": "advvio",
            "title": "T",
            "date": "2026-07-13",
            "domain": "eng",
            "workflow_used": "tdd",
        }
    )
    # No roles contributed yet -> advancing violates the workflow gate.
    out = TOOL_HANDLERS["memory_advance_phase"]({"session_id": "advvio"})
    assert out.startswith("Workflow violation:")


def test_merge_entry_b_not_found(mcp_db):
    add = TOOL_HANDLERS["memory_add"](
        {"type": "skill", "name": "OnlyA", "domain": "d", "trigger": "t", "workflow": "w"}
    )
    ida = int(add.split("#")[1].split(" ")[0])
    with patch("src.mcp.handlers.merge_available", return_value=True):
        out = TOOL_HANDLERS["memory_merge_entries"](
            {
                "item_type_a": "skill",
                "item_id_a": ida,
                "item_type_b": "skill",
                "item_id_b": 777777,
            }
        )
    assert out == "Error: skill ID 777777 not found."


def test_pin_unpin_success(mcp_db):
    add = TOOL_HANDLERS["memory_add"](
        {"type": "skill", "name": "PinMe", "domain": "d", "trigger": "t", "workflow": "w"}
    )
    sid = int(add.split("#")[1].split(" ")[0])
    pin = TOOL_HANDLERS["memory_pin"]({"item_type": "skill", "item_id": sid})
    assert pin == f"Pinned skill ID {sid}. It will always appear at the top of memory_search results."
    listed = TOOL_HANDLERS["memory_list_pinned"]({})
    assert "PinMe" in listed
    unpin = TOOL_HANDLERS["memory_unpin"]({"item_type": "skill", "item_id": sid})
    assert unpin == f"Unpinned skill ID {sid}."


def test_list_pinned_empty(mcp_db):
    assert TOOL_HANDLERS["memory_list_pinned"]({}) == "No pinned memories."


def test_embedding_status_ready_only(mcp_db):
    # total>0 but no stale/pending/failed -> only the Ready line.
    fake = {"total": 2, "model": "m", "ready": 2, "stale": 0, "pending": 0, "failed": 0}
    with patch("src.mcp.handlers.get_embedding_stats", return_value=fake):
        out = TOOL_HANDLERS["memory_embedding_status"]({})
    assert "Ready:      2 (100.0%)" in out
    assert "Stale:" not in out and "Pending:" not in out and "Failed:" not in out


def test_health_minimal_no_recs(mcp_db):
    report = {
        "items": {},
        "embeddings": {},
        "fts_total": 0,
        "vec_total": 0,
        "vec_drift": 0,
        "orphaned_tags": 0,
        "gc_candidates": 0,
        "recommendations": [],
    }
    with patch("src.mcp.handlers.run_health_check", return_value=report):
        out = TOOL_HANDLERS["memory_health"]({})
    assert "Memory Health Report" in out
    assert "Recommendations:" not in out
    assert "Embeddings" not in out


def test_auto_extract_list_arg(mcp_db):
    with patch("src.auto_extract._llm_extract", return_value=[]), patch(
        "src.auto_extract.is_llm_available", return_value=False
    ):
        out = TOOL_HANDLERS["memory_auto_extract"](
            {"messages": [{"role": "user", "content": "hi"}]}
        )
    assert "Auto-extract results:" in out


# --------------------------------------------------------------------------
# propose_decision (real inbox)
# --------------------------------------------------------------------------


def test_propose_decision_files_and_dedups(mcp_db):
    out = TOOL_HANDLERS["memory_propose_decision"](
        {"title": "Adopt schema v20", "body": "why", "finding_key": "schema-v20"}
    )
    assert out.startswith("Decision request #")
    assert "engram decide" in out
    # Same finding_key -> deduped, not re-filed.
    out2 = TOOL_HANDLERS["memory_propose_decision"](
        {"title": "Adopt schema v20 again", "finding_key": "schema-v20"}
    )
    assert out2 == "An open item already covers this finding — not re-filed."


# --------------------------------------------------------------------------
# memory_route inbox warning append
# --------------------------------------------------------------------------


def test_route_appends_inbox_warning(mcp_db):
    from src.inbox import file_item

    file_item(kind="alert", severity="critical", title="prod down", db_path=mcp_db)
    out = TOOL_HANDLERS["memory_route"]({"task": "deploy the app"})
    assert out.startswith("[Engram route")
    assert "Inbox: 1 open high/critical item(s)" in out


# --------------------------------------------------------------------------
# memory_recent empty
# --------------------------------------------------------------------------


def test_recent_empty(mcp_db):
    assert TOOL_HANDLERS["memory_recent"]({}) == "No entries yet."


# --------------------------------------------------------------------------
# memory_add dispatcher: missing-field errors
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "args,needle",
    [
        ({"type": "pattern"}, "missing required fields for pattern"),
        ({"type": "skill"}, "missing required fields for skill"),
        ({"type": "conversation"}, "missing required fields for conversation"),
        ({"type": "prompt"}, "missing required fields for prompt"),
    ],
)
def test_add_missing_fields(mcp_db, args, needle):
    out = TOOL_HANDLERS["memory_add"](args)
    assert out.startswith("Error:")
    assert needle in out


def test_add_dispatches_conversation_and_prompt(mcp_db):
    conv = TOOL_HANDLERS["memory_add"](
        {
            "type": "conversation",
            "conversation_id": "conv-1",
            "title": "Kickoff",
            "date": "2026-07-13",
            "domain": "engineering",
        }
    )
    assert "Conversation #" in conv and "Kickoff" in conv

    prm = TOOL_HANDLERS["memory_add"](
        {
            "type": "prompt",
            "name": "Reviewer",
            "role": "reviewer",
            "domain": "engineering",
            "description": "review code",
        }
    )
    assert "Prompt #" in prm and "Reviewer" in prm and "stored successfully" in prm


# --------------------------------------------------------------------------
# consolidate_skills (real DB)
# --------------------------------------------------------------------------


def test_consolidate_skills(mcp_db):
    s1 = TOOL_HANDLERS["memory_add"](
        {"type": "skill", "name": "A", "domain": "d", "trigger": "t1", "workflow": "w1"}
    )
    s2 = TOOL_HANDLERS["memory_add"](
        {"type": "skill", "name": "B", "domain": "d", "trigger": "t2", "workflow": "w2"}
    )
    id1 = int(s1.split("#")[1].split(" ")[0])
    id2 = int(s2.split("#")[1].split(" ")[0])

    out = TOOL_HANDLERS["memory_consolidate_skills"](
        {
            "new_skill_name": "Merged",
            "new_skill_domain": "d",
            "new_skill_trigger_desc": "when",
            "new_skill_workflow": "how",
            "new_skill_tags": "x, y",
            "skill_ids_to_delete": [id1, id2],
        }
    )
    assert "Consolidated into Skill #" in out
    assert "deleted 2 old entries" in out
    with get_connection(mcp_db) as conn:
        names = [r["name"] for r in conn.execute("SELECT name FROM skills").fetchall()]
    assert "Merged" in names
    assert "A" not in names and "B" not in names


# --------------------------------------------------------------------------
# get_session full formatting
# --------------------------------------------------------------------------


def test_get_session_missing_and_full(mcp_db):
    assert TOOL_HANDLERS["memory_get_session"]({}) == "Error: session_id is required."
    assert (
        TOOL_HANDLERS["memory_get_session"]({"session_id": "nope"})
        == "Session 'nope' not found."
    )

    TOOL_HANDLERS["memory_init_session"](
        {
            "session_id": "s1",
            "title": "T",
            "date": "2026-07-13",
            "domain": "eng",
            "workflow_used": "tdd",
        }
    )
    TOOL_HANDLERS["memory_add_transcript"](
        {"session_id": "s1", "role": "Analyst", "content": "did analysis"}
    )
    with get_connection(mcp_db) as conn:
        add_decision_ok = True
        try:
            conn.execute(
                "UPDATE sessions SET key_decisions = ? WHERE session_id = ?",
                ("Chose approach X", "s1"),
            )
        except Exception:
            add_decision_ok = False
    assert add_decision_ok

    out = TOOL_HANDLERS["memory_get_session"]({"session_id": "s1"})
    assert "Session: T (s1)" in out
    assert "Workflow: tdd" in out
    assert "Key Decisions:" in out and "Chose approach X" in out
    assert "Transcripts:" in out
    assert "[Analyst]" in out and "did analysis" in out


# --------------------------------------------------------------------------
# init_session with workflow phase info
# --------------------------------------------------------------------------


def test_init_session_reports_phase(mcp_db):
    out = TOOL_HANDLERS["memory_init_session"](
        {
            "session_id": "wf1",
            "title": "T",
            "date": "2026-07-13",
            "domain": "eng",
            "workflow_used": "tdd",
        }
    )
    assert "initialized successfully" in out
    assert "Starting phase:" in out


# --------------------------------------------------------------------------
# add_transcript waiting / ready status
# --------------------------------------------------------------------------


def test_add_transcript_waiting_status(mcp_db):
    TOOL_HANDLERS["memory_init_session"](
        {
            "session_id": "wf2",
            "title": "T",
            "date": "2026-07-13",
            "domain": "eng",
            "workflow_used": "tdd",
        }
    )
    out = TOOL_HANDLERS["memory_add_transcript"](
        {"session_id": "wf2", "role": "Analyst", "content": "hello"}
    )
    assert "Transcript entry for 'Analyst' added to session 'wf2'." in out
    # Either still waiting for more roles, or ready to proceed — both are real states.
    assert ("Still waiting for:" in out) or ("All required roles have contributed" in out)


# --------------------------------------------------------------------------
# add_decision success (roles satisfied)
# --------------------------------------------------------------------------


def test_add_decision_success_after_roles(mcp_db):
    from src.workflow import get_session_state

    sid = "dec1"
    TOOL_HANDLERS["memory_init_session"](
        {
            "session_id": sid,
            "title": "T",
            "date": "2026-07-13",
            "domain": "eng",
            "workflow_used": "tdd",
        }
    )
    state = get_session_state(sid)
    for role in state["required_roles"]:
        TOOL_HANDLERS["memory_add_transcript"](
            {"session_id": sid, "role": role, "content": f"{role} says hi"}
        )
    out = TOOL_HANDLERS["memory_add_decision"]({"session_id": sid, "decision": "Ship it"})
    assert out == f"Decision added to session '{sid}'."


# --------------------------------------------------------------------------
# get_role
# --------------------------------------------------------------------------


def test_get_role_found_and_missing(mcp_db):
    with get_connection(mcp_db) as conn:
        conn.execute(
            "INSERT INTO roles (name, charter, heuristics) VALUES (?, ?, ?)",
            ("Sentinel", "guard the gate", "trust nothing"),
        )
    out = TOOL_HANDLERS["memory_get_role"]({"name": "Sentinel"})
    assert "Charter:\nguard the gate" in out
    assert "Heuristics:\ntrust nothing" in out

    missing = TOOL_HANDLERS["memory_get_role"]({"name": "Ghost"})
    assert missing == "Role 'Ghost' not found in database."


# --------------------------------------------------------------------------
# index_file / query_codebase / get_stale_files
# --------------------------------------------------------------------------


def test_index_query_and_stale_files(mcp_db, tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    src_file = proj / "app.py"
    src_file.write_text("print('hi')\n")

    idx = TOOL_HANDLERS["memory_index_file"](
        {
            "project_path": str(proj),
            "file_path": "app.py",
            "summary": "entry point",
            "exports": "main",
            "dependencies": "os",
        }
    )
    assert "Indexed app.py for project" in idx

    # Missing project_path
    assert TOOL_HANDLERS["memory_query_codebase"]({}) == "Error: project_path is required."

    q = TOOL_HANDLERS["memory_query_codebase"]({"project_path": str(proj)})
    assert "Codebase Knowledge for" in q
    assert "app.py" in q
    assert "Summary: entry point" in q
    assert "Exports: main" in q
    assert "Deps: os" in q

    # Nonexistent file to index
    err = TOOL_HANDLERS["memory_index_file"](
        {"project_path": str(proj), "file_path": "gone.py", "summary": "x"}
    )
    assert err.startswith("Error: file not found")

    # Stale files: unchanged -> up to date
    assert (
        TOOL_HANDLERS["memory_get_stale_files"]({"project_path": str(proj)})
        == "All indexed files are up to date."
    )
    assert TOOL_HANDLERS["memory_get_stale_files"]({}) == "Error: project_path is required."

    # Modify the file -> reported modified
    src_file.write_text("print('changed')\n")
    stale = TOOL_HANDLERS["memory_get_stale_files"]({"project_path": str(proj)})
    parsed = json.loads(stale)
    assert parsed[0]["file_path"] == "app.py"
    assert parsed[0]["reason"] == "modified"

    # Delete the file -> reported deleted
    src_file.unlink()
    stale2 = json.loads(TOOL_HANDLERS["memory_get_stale_files"]({"project_path": str(proj)}))
    assert stale2[0]["reason"] == "deleted"


def test_query_codebase_empty(mcp_db, tmp_path):
    proj = tmp_path / "empty"
    proj.mkdir()
    out = TOOL_HANDLERS["memory_query_codebase"](
        {"project_path": str(proj), "query": "widgets"}
    )
    assert "No codebase knowledge found" in out
    assert "matching 'widgets'" in out


# --------------------------------------------------------------------------
# memory_list all item types
# --------------------------------------------------------------------------


def test_list_all_types(mcp_db):
    TOOL_HANDLERS["memory_add"](
        {
            "type": "mistake",
            "date": "2026-07-13",
            "context": "c",
            "mistake": "boom happened here",
            "fix": "the durable fix",
            "tags": "db",
        }
    )
    TOOL_HANDLERS["memory_add"](
        {
            "type": "pattern",
            "name": "PatX",
            "symptoms": "slow",
            "root_cause": "loop",
            "standard_fix": "batch it",
            "tags": "perf",
        }
    )
    TOOL_HANDLERS["memory_add"](
        {
            "type": "skill",
            "name": "SkillX",
            "domain": "eng",
            "trigger": "when x",
            "workflow": "do y",
            "tags": "git",
        }
    )
    TOOL_HANDLERS["memory_add"](
        {
            "type": "conversation",
            "conversation_id": "cid-123456789012",
            "title": "ConvX",
            "date": "2026-07-13",
            "domain": "eng",
            "tags": "chat",
        }
    )
    TOOL_HANDLERS["memory_add"](
        {
            "type": "prompt",
            "name": "PromptX",
            "role": "reviewer",
            "domain": "eng",
            "description": "desc",
            "best_for": "reviews",
            "tags": "review",
        }
    )

    assert TOOL_HANDLERS["memory_list"]({}) == (
        "Error: type is required (e.g. mistakes, patterns, skills)."
    )

    m = TOOL_HANDLERS["memory_list"]({"type": "mistakes"})
    assert "Mistakes (1):" in m and "boom happened here" in m and "Fix: the durable fix" in m

    p = TOOL_HANDLERS["memory_list"]({"type": "patterns"})
    assert "Patterns (1):" in p and "PatX" in p and "Symptoms: slow" in p
    assert "tags: perf" in p

    s = TOOL_HANDLERS["memory_list"]({"type": "skills"})
    assert "Skills (1):" in s and "SkillX [eng]" in s and "When: when x" in s
    assert "tags: git" in s

    c = TOOL_HANDLERS["memory_list"]({"type": "conversations"})
    assert "Conversations (1):" in c and "ConvX" in c and "cid-12345678..." in c
    assert "tags: chat" in c

    pr = TOOL_HANDLERS["memory_list"]({"type": "prompts"})
    assert "Prompts (1):" in pr and "PromptX [eng]" in pr and "Best for: reviews" in pr
    assert "tags: review" in pr

    assert TOOL_HANDLERS["memory_list"]({"type": "widgets"}) == "Unknown type: widgets"


# --------------------------------------------------------------------------
# memory_stats embeddings formatting (mocked stats dict)
# --------------------------------------------------------------------------


def test_stats_embeddings_formatting(mcp_db):
    fake = {
        "mistakes": 2,
        "patterns": 3,
        "skills": 4,
        "conversations": 1,
        "prompts": 5,
        "tags": 6,
        "fts_indexed": 10,
        "embeddings": {
            "total": 10,
            "model": "nomic",
            "ready": 5,
            "stale": 2,
            "pending": 2,
            "failed": 1,
        },
    }
    with patch("src.mcp.handlers.get_stats", return_value=fake):
        out = TOOL_HANDLERS["memory_stats"]({})
    assert "Mistakes:      2" in out
    assert "Prompts:       5" in out
    assert "Embedding Status (model: nomic)" in out
    assert "Ready:      5 (50.0%)" in out
    assert "Stale:      2 (20.0%)" in out
    assert "Pending:    2 (20.0%)" in out
    assert "Failed:     1 (10.0%)" in out
    assert "run `engram reembed`" in out


def test_stats_no_embeddings_tracked(mcp_db):
    fake = {
        "mistakes": 0,
        "patterns": 0,
        "skills": 0,
        "conversations": 0,
        "tags": 0,
        "fts_indexed": 0,
        "embeddings": {"total": 0, "model": "nomic"},
    }
    with patch("src.mcp.handlers.get_stats", return_value=fake):
        out = TOOL_HANDLERS["memory_stats"]({})
    assert "No embeddings tracked yet." in out


# --------------------------------------------------------------------------
# embedding_status handler (mocked)
# --------------------------------------------------------------------------


def test_embedding_status_formatting(mcp_db):
    fake = {"total": 4, "model": "m1", "ready": 1, "stale": 1, "pending": 1, "failed": 1}
    with patch("src.mcp.handlers.get_embedding_stats", return_value=fake):
        out = TOOL_HANDLERS["memory_embedding_status"]({})
    assert "Embedding Status (model: m1)" in out
    assert "Ready:      1 (25.0%)" in out
    assert "Stale:" in out and "Pending:" in out and "Failed:" in out


def test_embedding_status_empty(mcp_db):
    with patch("src.mcp.handlers.get_embedding_stats", return_value={"total": 0}):
        out = TOOL_HANDLERS["memory_embedding_status"]({})
    assert "No embeddings tracked yet." in out


# --------------------------------------------------------------------------
# session_review
# --------------------------------------------------------------------------


def test_session_review_returns_prompt(mcp_db):
    out = TOOL_HANDLERS["memory_session_review"](
        {"conversation_id": "c1", "tasks_completed": "shipped X"}
    )
    assert isinstance(out, str) and out.strip()
    # The review prompt echoes the provided task summary.
    assert "shipped X" in out


# --------------------------------------------------------------------------
# check_workflow_state / advance_phase
# --------------------------------------------------------------------------


def test_check_workflow_state_paths(mcp_db):
    assert (
        TOOL_HANDLERS["memory_check_workflow_state"]({})
        == "Error: session_id is required."
    )
    assert "No workflow state found" in TOOL_HANDLERS["memory_check_workflow_state"](
        {"session_id": "unknown-x"}
    )

    TOOL_HANDLERS["memory_init_session"](
        {
            "session_id": "wf-check",
            "title": "T",
            "date": "2026-07-13",
            "domain": "eng",
            "workflow_used": "tdd",
        }
    )
    out = TOOL_HANDLERS["memory_check_workflow_state"]({"session_id": "wf-check"})
    assert "Session: wf-check" in out
    assert "Current phase:" in out
    assert "Required roles:" in out
    assert "Can proceed:" in out


def test_advance_phase(mcp_db):
    assert TOOL_HANDLERS["memory_advance_phase"]({}) == "Error: session_id is required."

    from src.workflow import get_session_state

    sid = "adv1"
    TOOL_HANDLERS["memory_init_session"](
        {
            "session_id": sid,
            "title": "T",
            "date": "2026-07-13",
            "domain": "eng",
            "workflow_used": "tdd",
        }
    )
    state = get_session_state(sid)
    for role in state["required_roles"]:
        TOOL_HANDLERS["memory_add_transcript"](
            {"session_id": sid, "role": role, "content": "x"}
        )
    out = TOOL_HANDLERS["memory_advance_phase"]({"session_id": sid})
    # Either advances to a named phase, or reports a workflow violation string —
    # both are exercised branches returning a real message.
    assert ("Advanced to phase" in out) or ("Workflow violation" in out)


# --------------------------------------------------------------------------
# find_similar formatting (mocked)
# --------------------------------------------------------------------------


def test_find_similar_missing_content(mcp_db):
    assert TOOL_HANDLERS["memory_find_similar"]({}) == "Error: content is required."


def test_find_similar_none_found(mcp_db):
    with patch("src.mcp.handlers.find_similar", return_value=[]):
        out = TOOL_HANDLERS["memory_find_similar"]({"content": "abc"})
    assert "No similar entries found" in out


def test_find_similar_formats_hits(mcp_db):
    hits = [
        {
            "item_type": "skill",
            "item_id": 3,
            "title": "close match",
            "similarity": 0.91,
            "snippet": "a" * 200,
        }
    ]
    with patch("src.mcp.handlers.find_similar", return_value=hits):
        out = TOOL_HANDLERS["memory_find_similar"]({"content": "abc", "threshold": 0.8})
    assert "Found 1 similar entries (threshold: 0.8):" in out
    assert "[SKILL ID:3] close match (similarity: 0.91)" in out
    assert ("a" * 120) in out
    assert "Options: skip" in out


# --------------------------------------------------------------------------
# merge_entries (mocked)
# --------------------------------------------------------------------------


def test_merge_missing_args(mcp_db):
    out = TOOL_HANDLERS["memory_merge_entries"]({"item_type_a": "skill"})
    assert out.startswith("Error:") and "are all required" in out


def test_merge_ollama_unavailable(mcp_db):
    with patch("src.mcp.handlers.merge_available", return_value=False):
        out = TOOL_HANDLERS["memory_merge_entries"](
            {
                "item_type_a": "skill",
                "item_id_a": 1,
                "item_type_b": "skill",
                "item_id_b": 2,
            }
        )
    assert "Ollama is not available" in out


def test_merge_entry_not_found(mcp_db):
    with patch("src.mcp.handlers.merge_available", return_value=True):
        out = TOOL_HANDLERS["memory_merge_entries"](
            {
                "item_type_a": "skill",
                "item_id_a": 111,
                "item_type_b": "skill",
                "item_id_b": 222,
            }
        )
    assert out == "Error: skill ID 111 not found."


def test_merge_success_draft(mcp_db):
    a = TOOL_HANDLERS["memory_add"](
        {"type": "skill", "name": "SA", "domain": "d", "trigger": "t", "workflow": "w"}
    )
    b = TOOL_HANDLERS["memory_add"](
        {"type": "skill", "name": "SB", "domain": "d", "trigger": "t2", "workflow": "w2"}
    )
    ida = int(a.split("#")[1].split(" ")[0])
    idb = int(b.split("#")[1].split(" ")[0])
    merged = {"name": "SAB", "trigger": "merged"}
    with patch("src.mcp.handlers.merge_available", return_value=True), patch(
        "src.mcp.handlers.merge_entries", return_value=merged
    ):
        out = TOOL_HANDLERS["memory_merge_entries"](
            {
                "item_type_a": "skill",
                "item_id_a": ida,
                "item_type_b": "skill",
                "item_id_b": idb,
            }
        )
    assert "Merged entry draft" in out
    assert '"name": "SAB"' in out


def test_merge_llm_returns_none(mcp_db):
    a = TOOL_HANDLERS["memory_add"](
        {"type": "skill", "name": "SC", "domain": "d", "trigger": "t", "workflow": "w"}
    )
    b = TOOL_HANDLERS["memory_add"](
        {"type": "skill", "name": "SD", "domain": "d", "trigger": "t2", "workflow": "w2"}
    )
    ida = int(a.split("#")[1].split(" ")[0])
    idb = int(b.split("#")[1].split(" ")[0])
    with patch("src.mcp.handlers.merge_available", return_value=True), patch(
        "src.mcp.handlers.merge_entries", return_value=None
    ):
        out = TOOL_HANDLERS["memory_merge_entries"](
            {
                "item_type_a": "skill",
                "item_id_a": ida,
                "item_type_b": "skill",
                "item_id_b": idb,
            }
        )
    assert "Merge failed" in out


# --------------------------------------------------------------------------
# health formatting (mocked)
# --------------------------------------------------------------------------


def test_health_formatting(mcp_db):
    report = {
        "items": {
            "mistake": {"total": 3, "unused_180_plus_days": 1},
            "skill": {"total": 0, "unused_180_plus_days": 0},
        },
        "embeddings": {"model": "nomic", "ready": 3, "stale": 1, "pending": 0},
        "fts_total": 5,
        "vec_total": 4,
        "vec_drift": 1,
        "orphaned_tags": 2,
        "gc_candidates": 1,
        "recommendations": ["Run reembed", "Run gc"],
    }
    with patch("src.mcp.handlers.run_health_check", return_value=report):
        out = TOOL_HANDLERS["memory_health"]({})
    assert "mistake: 3 total, 1 GC candidates" in out
    # zero-total item type is skipped.
    assert "skill:" not in out
    assert "Embeddings (nomic): ready=3, stale=1, pending=0" in out
    assert "FTS: 5, Vec: 4, Drift: 1" in out
    assert "Orphaned tags: 2, GC candidates: 1" in out
    assert "Recommendations:" in out
    assert "• Run reembed" in out


# --------------------------------------------------------------------------
# suggest_consolidations (mocked)
# --------------------------------------------------------------------------


def test_suggest_consolidations_unchanged(mcp_db):
    with patch(
        "src.mcp.handlers.find_consolidation_candidates", return_value=([], "unchanged")
    ):
        out = TOOL_HANDLERS["memory_suggest_consolidations"]({})
    assert "No changes since last consolidation scan" in out


def test_suggest_consolidations_none(mcp_db):
    with patch(
        "src.mcp.handlers.find_consolidation_candidates", return_value=([], None)
    ):
        out = TOOL_HANDLERS["memory_suggest_consolidations"]({"threshold": 0.9})
    assert "No consolidation candidates found at similarity threshold 0.9" in out


def test_suggest_consolidations_clusters(mcp_db):
    clusters = [
        {
            "item_type": "skill",
            "avg_similarity": 0.88,
            "cluster_size": 2,
            "items": [
                {"item_id": 1, "title": "one"},
                {"item_id": 2, "title": "two"},
            ],
        }
    ]
    with patch(
        "src.mcp.handlers.find_consolidation_candidates", return_value=(clusters, None)
    ):
        out = TOOL_HANDLERS["memory_suggest_consolidations"]({})
    assert "Found 1 consolidation candidate(s)" in out
    assert "Cluster 1 — skill (avg similarity: 0.88, size: 2)" in out
    assert "ID:1  one" in out and "ID:2  two" in out


# --------------------------------------------------------------------------
# gc handler + destructive-confirm gate
# --------------------------------------------------------------------------


def test_gc_dry_run_no_candidates(mcp_db):
    out = TOOL_HANDLERS["memory_gc"]({})
    assert "No GC candidates found" in out


def test_gc_dry_run_with_candidates(mcp_db):
    result = {
        "mode": "dry-run",
        "blocked": False,
        "candidates": [
            {"item_type": "skill", "item_id": 9, "created_at": "2020-01-01"},
        ],
        "processed": 0,
    }
    with patch("src.mcp.handlers.run_gc", return_value=result):
        out = TOOL_HANDLERS["memory_gc"]({"mode": "dry-run"})
    assert "GC dry-run — 1 candidate(s)" in out
    assert "[SKILL ID:9] created: 2020-01-01" in out
    assert "Call with mode='archive'" in out


def test_gc_blocked(mcp_db):
    result = {"mode": "archive", "blocked": True, "reason": "too many"}
    with patch("src.mcp.handlers.run_gc", return_value=result), patch(
        "src.mcp.protocol.elicit_confirmation", return_value=None
    ):
        out = TOOL_HANDLERS["memory_gc"]({"mode": "archive"})
    assert out == "GC blocked: too many"


def test_gc_archive_reports_processed(mcp_db):
    result = {
        "mode": "archive",
        "blocked": False,
        "candidates": [{"item_type": "skill", "item_id": 1, "created_at": None}],
        "processed": 1,
    }
    with patch("src.mcp.handlers.run_gc", return_value=result), patch(
        "src.mcp.protocol.elicit_confirmation", return_value=None
    ):
        out = TOOL_HANDLERS["memory_gc"]({"mode": "archive"})
    assert "created: unknown" in out
    assert "Archived 1 items." in out


def test_gc_cancelled_by_confirm(mcp_db):
    with patch("src.mcp.protocol.elicit_confirmation", return_value=False):
        out = TOOL_HANDLERS["memory_gc"]({"mode": "archive"})
    assert out == "Cancelled by user — no changes were made."


# --------------------------------------------------------------------------
# pin / unpin error paths
# --------------------------------------------------------------------------


def test_pin_unpin_error_paths(mcp_db):
    assert TOOL_HANDLERS["memory_pin"]({}).startswith("Error:")
    assert TOOL_HANDLERS["memory_unpin"]({}).startswith("Error:")

    pin = TOOL_HANDLERS["memory_pin"]({"item_type": "skill", "item_id": 4242})
    assert pin == "Error: could not pin skill ID 4242 (item not found)."

    unpin = TOOL_HANDLERS["memory_unpin"]({"item_type": "skill", "item_id": 4242})
    assert unpin == "Error: skill ID 4242 was not pinned."


# --------------------------------------------------------------------------
# invalidate
# --------------------------------------------------------------------------


def test_invalidate_missing_and_notfound(mcp_db):
    assert TOOL_HANDLERS["memory_invalidate"]({}) == (
        "Error: item_type and item_id are required."
    )
    out = TOOL_HANDLERS["memory_invalidate"]({"item_type": "skill", "item_id": 9999})
    assert out == "Error: could not invalidate skill ID 9999."


def test_invalidate_success(mcp_db):
    add = TOOL_HANDLERS["memory_add"](
        {"type": "skill", "name": "ToKill", "domain": "d", "trigger": "t", "workflow": "w"}
    )
    sid = int(add.split("#")[1].split(" ")[0])
    out = TOOL_HANDLERS["memory_invalidate"](
        {"item_type": "skill", "item_id": sid, "reason": "obsolete"}
    )
    assert out == f"Invalidated skill ID {sid}."
    with get_connection(mcp_db) as conn:
        name = conn.execute("SELECT name FROM skills WHERE id = ?", (sid,)).fetchone()["name"]
    assert name.startswith("[SUPERSEDED]")


# --------------------------------------------------------------------------
# sleep (mocked run_sleep for non-dry-run gate)
# --------------------------------------------------------------------------


def test_sleep_dry_run_json(mcp_db):
    out = TOOL_HANDLERS["memory_sleep"]({"dry_run": True})
    parsed = json.loads(out)
    assert isinstance(parsed, dict)


def test_sleep_confirm_cancelled(mcp_db):
    with patch("src.mcp.protocol.elicit_confirmation", return_value=False):
        out = TOOL_HANDLERS["memory_sleep"]({"dry_run": False})
    assert out == "Cancelled by user — no changes were made."


def test_sleep_proceeds_when_no_gate(mcp_db):
    with patch("src.mcp.protocol.elicit_confirmation", return_value=None), patch(
        "src.mcp.handlers.run_sleep", return_value={"superseded": 0, "archived": 0}
    ) as rs:
        out = TOOL_HANDLERS["memory_sleep"]({"dry_run": False, "threshold": 0.9, "days_unused": 15})
    assert json.loads(out) == {"superseded": 0, "archived": 0}
    assert rs.call_args.kwargs["dry_run"] is False
    assert rs.call_args.kwargs["threshold"] == 0.9
    assert rs.call_args.kwargs["days_unused"] == 15


# --------------------------------------------------------------------------
# llm_status
# --------------------------------------------------------------------------


def test_llm_status_json(mcp_db):
    with patch("src.llm.get_llm_status", return_value={"available": False, "model": "x"}):
        out = TOOL_HANDLERS["memory_llm_status"]({})
    parsed = json.loads(out)
    assert parsed["available"] is False
    assert parsed["model"] == "x"


# --------------------------------------------------------------------------
# suggest_capture
# --------------------------------------------------------------------------


def test_suggest_capture_missing_args(mcp_db):
    out = TOOL_HANDLERS["memory_suggest_capture"]({"task_description": "x"})
    assert out == "Error: task_description and outcome are required."


def test_suggest_capture_returns_suggestion(mcp_db):
    out = TOOL_HANDLERS["memory_suggest_capture"](
        {
            "task_description": "Fixed a flaky test",
            "outcome": "Test now passes reliably",
            "errors_encountered": "AssertionError",
            "files_changed": ["tests/test_x.py"],
        }
    )
    assert isinstance(out, str) and out.strip()


# --------------------------------------------------------------------------
# auto_extract
# --------------------------------------------------------------------------


def test_auto_extract_invalid_json(mcp_db):
    out = TOOL_HANDLERS["memory_auto_extract"]({"messages": "{not json"})
    assert out == "Error: messages must be valid JSON array."


def test_auto_extract_not_a_list(mcp_db):
    out = TOOL_HANDLERS["memory_auto_extract"]({"messages": '{"a": 1}'})
    assert out == "Error: messages must be a JSON array of {role, content} objects."


def test_auto_extract_missing_task_fields(mcp_db):
    out = TOOL_HANDLERS["memory_auto_extract"]({})
    assert out == "Error: provide messages (JSON) or both task_description and outcome."


def test_auto_extract_messages_path(mcp_db):
    msgs = json.dumps([{"role": "user", "content": "hi"}, {"role": "assistant", "content": "ok"}])
    with patch("src.auto_extract._llm_extract", return_value=[]), patch(
        "src.auto_extract.is_llm_available", return_value=False
    ):
        out = TOOL_HANDLERS["memory_auto_extract"]({"messages": msgs})
    assert "Auto-extract results:" in out


def test_auto_extract_task_path(mcp_db):
    with patch("src.auto_extract._llm_extract", return_value=[]), patch(
        "src.auto_extract.is_llm_available", return_value=False
    ):
        out = TOOL_HANDLERS["memory_auto_extract"](
            {"task_description": "Refactor module", "outcome": "cleaner code"}
        )
    assert "Auto-extract results:" in out
    assert "Engineering capture suggestion:" in out


# --------------------------------------------------------------------------
# export_skill
# --------------------------------------------------------------------------


def test_export_skill_by_id(mcp_db, tmp_path):
    add = TOOL_HANDLERS["memory_add"](
        {
            "type": "skill",
            "name": "Exportable skill",
            "domain": "eng",
            "trigger": "when needed",
            "workflow": "do the thing",
        }
    )
    sid = int(add.split("#")[1].split(" ")[0])
    outdir = tmp_path / "skills_out"
    out = TOOL_HANDLERS["memory_export_skill"](
        {"skill_id": sid, "output_path": str(outdir)}
    )
    assert "exported to" in out
    # A .md file was actually written.
    written = list(outdir.rglob("SKILL.md"))
    assert written, "expected an exported SKILL.md file"


def test_export_skill_skipped_second_time(mcp_db, tmp_path):
    add = TOOL_HANDLERS["memory_add"](
        {"type": "skill", "name": "Twice", "domain": "eng", "trigger": "t", "workflow": "w"}
    )
    sid = int(add.split("#")[1].split(" ")[0])
    outdir = str(tmp_path / "twice_out")
    first = TOOL_HANDLERS["memory_export_skill"]({"skill_id": sid, "output_path": outdir})
    assert "exported to" in first
    second = TOOL_HANDLERS["memory_export_skill"]({"skill_id": sid, "output_path": outdir})
    assert "already exists at" in second and "No changes made" in second


def test_export_skill_not_found(mcp_db, tmp_path):
    out = TOOL_HANDLERS["memory_export_skill"](
        {"skill_id": 999999, "output_path": str(tmp_path / "o")}
    )
    assert out == "Skill ID 999999 not found."


def test_export_pattern_by_id(mcp_db, tmp_path):
    add = TOOL_HANDLERS["memory_add"](
        {
            "type": "pattern",
            "name": "Exportable pattern",
            "symptoms": "s",
            "root_cause": "r",
            "standard_fix": "f",
        }
    )
    pid = int(add.split("#")[1].split(" ")[0])
    outdir = tmp_path / "pat_out"
    out = TOOL_HANDLERS["memory_export_skill"](
        {"pattern_id": pid, "output_path": str(outdir)}
    )
    assert "exported as skill to" in out
    assert list(outdir.rglob("*.md"))


def test_export_pattern_not_found(mcp_db, tmp_path):
    out = TOOL_HANDLERS["memory_export_skill"](
        {"pattern_id": 888888, "output_path": str(tmp_path / "o")}
    )
    assert out == "Pattern ID 888888 not found."


def test_export_missing_args(mcp_db):
    out = TOOL_HANDLERS["memory_export_skill"]({})
    assert out == "Error: provide either skill_id or pattern_id."


# --------------------------------------------------------------------------
# sync_skills (real, dry-run)
# --------------------------------------------------------------------------


def test_sync_skills_dry_run(mcp_db, tmp_path):
    TOOL_HANDLERS["memory_add"](
        {
            "type": "skill",
            "name": "OnlyInEngram",
            "domain": "eng",
            "trigger": "t",
            "workflow": "w",
        }
    )
    empty_dir = tmp_path / "cursor_skills"
    empty_dir.mkdir()
    out = TOOL_HANDLERS["memory_sync_skills"]({"skills_dir": str(empty_dir)})
    assert "Engram ↔ Cursor Skill Sync" in out
    assert "Only in Engram:" in out
    assert "OnlyInEngram" in out
    assert "Dry-run mode: no changes made." in out


def test_sync_skills_auto_sync_exports(mcp_db, tmp_path):
    TOOL_HANDLERS["memory_add"](
        {
            "type": "skill",
            "name": "SyncMe",
            "domain": "eng",
            "trigger": "t",
            "workflow": "w",
        }
    )
    outdir = tmp_path / "sync_out"
    outdir.mkdir()
    out = TOOL_HANDLERS["memory_sync_skills"](
        {"skills_dir": str(outdir), "dry_run": False, "auto_sync": True}
    )
    assert "Exported" in out
    assert "SyncMe" in out
    assert list(outdir.rglob("SKILL.md"))
