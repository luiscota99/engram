"""Tests for the reflex layer — promote / approve / run / MCP exposure."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from src.reflex import (
    approve_reflex,
    handle_reflex_call,
    list_reflexes,
    promote_skill,
    reflex_tools_for_mcp,
    run_reflex,
)


def _seed_skill(conn, name="Echo Greeting"):
    cur = conn.execute(
        "INSERT INTO skills (name, domain, trigger_desc, workflow) VALUES (?, 'ops', 't', ?)",
        (name, "echo hello"),
    )
    return cur.lastrowid


def test_promote_falls_back_to_template_without_llm(test_db):
    conn = test_db["conn"]
    sid = _seed_skill(conn)
    conn.commit()

    with patch("src.llm.is_llm_available", return_value=False):
        result = promote_skill(sid, db_path=test_db["path"])
    assert result["drafted_by"] == "template"
    assert result["name"] == "echo_greeting"

    rows = list_reflexes(db_path=test_db["path"])
    assert len(rows) == 1
    assert rows[0]["approved_at"] is None


def test_unapproved_reflex_will_not_run(test_db):
    conn = test_db["conn"]
    sid = _seed_skill(conn)
    conn.commit()
    with patch("src.llm.is_llm_available", return_value=False):
        r = promote_skill(sid, db_path=test_db["path"])

    result = run_reflex(r["id"], db_path=test_db["path"])
    assert result["ok"] is False
    assert "not approved" in result["error"].lower()


def test_approve_then_run_executes_and_passes_params(test_db):
    conn = test_db["conn"]
    sid = _seed_skill(conn)
    conn.commit()
    with patch("src.llm.is_llm_available", return_value=False):
        r = promote_skill(sid, db_path=test_db["path"])

    # Replace the template with a real script, then approve
    from src.database import get_connection

    with get_connection(test_db["path"]) as c:
        c.execute(
            "UPDATE reflexes SET script = ? WHERE id = ?",
            ('echo "hello $PARAM_WHO"', r["id"]),
        )
    approve_reflex(r["id"], db_path=test_db["path"])

    result = run_reflex(r["id"], params={"who": "world"}, db_path=test_db["path"])
    assert result["ok"] is True
    assert "hello world" in result["output"]

    # run bumps the underlying skill's usage (feeds the reuse metric)
    with get_connection(test_db["path"]) as c:
        usage = c.execute("SELECT usage_count FROM skills WHERE id = ?", (sid,)).fetchone()[0]
    assert usage == 1


def test_tampered_script_refuses_to_run(test_db):
    conn = test_db["conn"]
    sid = _seed_skill(conn)
    conn.commit()
    with patch("src.llm.is_llm_available", return_value=False):
        r = promote_skill(sid, db_path=test_db["path"])
    from src.database import get_connection

    with get_connection(test_db["path"]) as c:
        c.execute("UPDATE reflexes SET script = 'echo safe' WHERE id = ?", (r["id"],))
    approve_reflex(r["id"], db_path=test_db["path"])
    # Tamper after approval
    with get_connection(test_db["path"]) as c:
        c.execute("UPDATE reflexes SET script = 'echo EVIL' WHERE id = ?", (r["id"],))

    result = run_reflex(r["id"], db_path=test_db["path"])
    assert result["ok"] is False
    assert "changed since approval" in result["error"]


def test_only_approved_reflexes_are_exposed_as_mcp_tools(test_db):
    conn = test_db["conn"]
    sid = _seed_skill(conn)
    conn.commit()
    with patch("src.llm.is_llm_available", return_value=False):
        r = promote_skill(sid, db_path=test_db["path"])

    # Unapproved → not listed
    assert reflex_tools_for_mcp(db_path=test_db["path"]) == []

    from src.database import get_connection

    with get_connection(test_db["path"]) as c:
        c.execute("UPDATE reflexes SET script = 'echo ok' WHERE id = ?", (r["id"],))
    approve_reflex(r["id"], db_path=test_db["path"])

    tools = reflex_tools_for_mcp(db_path=test_db["path"])
    assert len(tools) == 1
    assert tools[0]["name"] == "reflex_echo_greeting"


def test_handle_reflex_call_routes_to_executor(test_db):
    conn = test_db["conn"]
    sid = _seed_skill(conn)
    conn.commit()
    with patch("src.llm.is_llm_available", return_value=False):
        r = promote_skill(sid, db_path=test_db["path"])
    from src.database import get_connection

    with get_connection(test_db["path"]) as c:
        c.execute("UPDATE reflexes SET script = ? WHERE id = ?", ('echo "from $PARAM_X"', r["id"]))
    approve_reflex(r["id"], db_path=test_db["path"])

    out = handle_reflex_call("reflex_echo_greeting", {"x": "mcp"}, db_path=test_db["path"])
    assert "from mcp" in out


def test_promote_missing_skill_raises(test_db):
    with pytest.raises(ValueError):
        promote_skill(9999, db_path=test_db["path"])
