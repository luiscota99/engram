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
