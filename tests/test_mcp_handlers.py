"""Parametrized tests for key MCP tool handlers."""

from __future__ import annotations

import json
import os

import pytest

from src.database import get_connection, init_db
from src.mcp.handlers import TOOL_HANDLERS
from src.workflow import init_session_state


@pytest.fixture
def mcp_db(tmp_path):
    """Temporary DB with ENGRAM_DB_PATH set (no open connection held)."""
    db_path = str(tmp_path / "mcp_handlers.db")
    os.environ["ENGRAM_DB_PATH"] = db_path
    init_db(db_path)
    return db_path


def _session_with_workflow_state(db_path: str, session_id: str = "wf-session") -> None:
    with get_connection(db_path) as conn:
        conn.execute(
            """INSERT INTO sessions (session_id, title, date, domain, workflow_used)
               VALUES (?, 'Test Session', '2026-01-01', 'engineering', NULL)""",
            (session_id,),
        )
    init_session_state(session_id, db_path=db_path)


@pytest.mark.parametrize(
    "tool_name,args,expected",
    [
        ("memory_search", {"query": ""}, {"no_crash": True}),
        (
            "memory_health",
            {},
            {"contains": "Memory Health Report"},
        ),
        (
            "memory_invalidate",
            {},
            {"startswith": "Error:"},
        ),
        (
            "memory_sleep",
            {"dry_run": True},
            {"valid_json": True},
        ),
    ],
)
def test_tool_handlers(mcp_db, tool_name, args, expected):
    handler = TOOL_HANDLERS[tool_name]
    result = handler(args)

    assert isinstance(result, str)

    if expected.get("no_crash"):
        assert result  # empty query returns a wrapped "No results found." string

    if "contains" in expected:
        assert expected["contains"] in result

    if "startswith" in expected:
        assert result.startswith(expected["startswith"])

    if expected.get("valid_json"):
        parsed = json.loads(result)
        assert isinstance(parsed, dict)


def test_memory_add_decision_workflow_violation_when_roles_missing(mcp_db):
    session_id = "decision-gate-session"
    _session_with_workflow_state(mcp_db, session_id)

    handler = TOOL_HANDLERS["memory_add_decision"]
    result = handler({"session_id": session_id, "decision": "Ship it."})

    assert "WorkflowViolation" in result
    assert "Analyst" in result


def test_format_results_rank_aware_snippets():
    """Top hit gets a 500-char snippet; lower ranks stay at 150."""
    from src.mcp.handlers import format_and_truncate_results

    long_text = "x" * 1000
    results = [
        {"item_type": "skill", "item_id": 1, "title": "top", "snippet": long_text, "tags": ""},
        {"item_type": "skill", "item_id": 2, "title": "second", "snippet": long_text, "tags": ""},
    ]
    out = format_and_truncate_results(results)
    blocks = out.split("[SKILL ID:")
    assert "x" * 500 in blocks[1] and "x" * 501 not in blocks[1]
    assert "x" * 150 in blocks[2] and "x" * 151 not in blocks[2]


def test_memory_search_default_limit_is_5(test_db):
    from unittest.mock import patch

    from src.mcp.handlers import handle_memory_search

    with patch("src.mcp.handlers.memory_search", return_value=[]) as ms:
        handle_memory_search({"query": "anything"})
    assert ms.call_args.kwargs["limit"] == 5


# --- Broad smoke coverage: every read-mostly handler returns a str, no crash ---


@pytest.mark.parametrize(
    "tool_name,args",
    [
        ("memory_recent", {}),
        ("memory_recent", {"count": 3, "type": "mistake"}),
        ("memory_stats", {}),
        ("memory_list", {}),
        ("memory_list", {"type": "skill"}),
        ("memory_list_pinned", {}),
        ("memory_llm_status", {}),
        ("memory_embedding_status", {}),
        ("memory_health", {}),
        ("memory_suggest_capture", {}),
        ("memory_suggest_consolidations", {}),
        ("memory_get_stale_files", {}),
        ("memory_query_codebase", {"query": "database"}),
        ("memory_route", {"task": "deploy the app"}),
        ("memory_recent", {"count": 0}),
        # Guard rails: missing required args must return an "Error:"/message str, not crash.
        ("memory_record_usage", {}),
        ("memory_read_item", {}),
        ("memory_read_item", {"item_type": "mistake", "item_id": 99999}),
        ("memory_route", {}),
        ("memory_propose_decision", {}),
        ("memory_add", {"type": "bogus"}),
        ("memory_add", {"type": "mistake"}),
        ("memory_merge_entries", {}),
        ("memory_find_similar", {}),
        ("memory_pin", {}),
        ("memory_unpin", {}),
    ],
)
def test_read_handlers_return_str_without_crashing(mcp_db, tool_name, args):
    result = TOOL_HANDLERS[tool_name](args)
    assert isinstance(result, str)
    assert result  # never an empty string


def test_add_mistake_roundtrip_search_read_and_record(mcp_db):
    """add(mistake) → search finds it → read_item returns JSON → record_usage boosts."""
    add_result = TOOL_HANDLERS["memory_add"](
        {
            "type": "mistake",
            "date": "2026-07-13",
            "context": "Mixing L2 and cosine vector norms",
            "mistake": "Compared un-normalized vectors under L2 distance",
            "fix": "Normalize embeddings before storing",
            "root_cause": "Two Ollama endpoints return different norms",
            "prevention": "Assert unit norm on the write path",
        }
    )
    assert "logged successfully" in add_result
    # Extract the id: "Mistake #<id> logged successfully."
    mid = int(add_result.split("#")[1].split(" ")[0])

    search_result = TOOL_HANDLERS["memory_search"]({"query": "vector norms", "type": "mistake"})
    assert "L2" in search_result or "norm" in search_result.lower()

    read_result = TOOL_HANDLERS["memory_read_item"]({"item_type": "mistake", "item_id": mid})
    parsed = json.loads(read_result)
    assert isinstance(parsed, dict)

    usage_result = TOOL_HANDLERS["memory_record_usage"](
        {"item_type": "mistake", "item_id": mid, "success": True}
    )
    assert "recorded usage" in usage_result


def test_add_pattern_and_skill_roundtrip(mcp_db):
    pat = TOOL_HANDLERS["memory_add"](
        {
            "type": "pattern",
            "name": "N+1 query blowup",
            "symptoms": "Latency scales with row count",
            "root_cause": "Per-row query in a loop",
            "standard_fix": "Batch with a single JOIN",
        }
    )
    assert "logged successfully" in pat

    skill = TOOL_HANDLERS["memory_add"](
        {
            "type": "skill",
            "name": "Bisect a regression",
            "domain": "engineering",
            "trigger": "A test passed last week and fails now",
            "workflow": "git bisect between the known-good and known-bad commits",
        }
    )
    assert "skill" in skill.lower()


def test_pin_unpin_roundtrip(mcp_db):
    add_result = TOOL_HANDLERS["memory_add"](
        {
            "type": "pattern",
            "name": "Pinnable pattern",
            "symptoms": "s",
            "root_cause": "r",
            "standard_fix": "f",
        }
    )
    pid = int(add_result.split("#")[1].split(" ")[0])

    pin_result = TOOL_HANDLERS["memory_pin"]({"item_type": "pattern", "item_id": pid})
    assert isinstance(pin_result, str) and pin_result

    listed = TOOL_HANDLERS["memory_list_pinned"]({})
    assert "Pinnable pattern" in listed or str(pid) in listed

    unpin_result = TOOL_HANDLERS["memory_unpin"]({"item_type": "pattern", "item_id": pid})
    assert isinstance(unpin_result, str) and unpin_result


def test_init_session_then_get_and_check_workflow(mcp_db):
    session_id = "roundtrip-session"
    init_result = TOOL_HANDLERS["memory_init_session"](
        {"session_id": session_id, "title": "Roundtrip", "date": "2026-07-13", "domain": "engineering"}
    )
    assert isinstance(init_result, str) and init_result

    get_result = TOOL_HANDLERS["memory_get_session"]({"session_id": session_id})
    assert isinstance(get_result, str) and get_result

    wf_result = TOOL_HANDLERS["memory_check_workflow_state"]({"session_id": session_id})
    assert isinstance(wf_result, str) and wf_result
