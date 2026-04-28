"""Tests for shared session review prompt (MCP + CLI parity)."""
from __future__ import annotations

import src.session_review as sr


def test_build_session_review_prompt_contains_checklist():
    t = sr.build_session_review_prompt(
        conversation_id="test-sess-1",
        project_path=None,
        tasks_completed="Did X",
        bugs_fixed="",
        new_patterns_noticed="",
        workflows_used="",
    )
    assert "Session Retrospective" in t
    assert "Reflection Checklist" in t
    assert "Did X" in t
    assert "Engram influence" in t or "0–3" in t


def test_mcp_and_module_same_output(test_db):
    """MCP handler should match build_session_review_prompt for same args."""
    from src.mcp_server import handle_memory_session_review

    args = {
        "conversation_id": "cid-1",
        "project_path": None,
        "tasks_completed": "t1",
        "bugs_fixed": "b1",
        "new_patterns_noticed": "p1",
        "workflows_used": "w1",
    }
    a = handle_memory_session_review(args)
    b = sr.build_session_review_prompt(
        conversation_id="cid-1",
        project_path=None,
        tasks_completed="t1",
        bugs_fixed="b1",
        new_patterns_noticed="p1",
        workflows_used="w1",
    )
    assert a == b
