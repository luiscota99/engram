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


def test_reflex_runs_history_and_success_rates(test_db):
    from src.database import get_connection
    from src.reflex import get_reflex_success_rates

    conn = test_db["conn"]
    sid = _seed_skill(conn, name="History Test")
    conn.commit()
    with patch("src.llm.is_llm_available", return_value=False):
        r = promote_skill(sid, db_path=test_db["path"])
    with get_connection(test_db["path"]) as c:
        c.execute("UPDATE reflexes SET script = 'echo fine' WHERE id = ?", (r["id"],))
    approve_reflex(r["id"], db_path=test_db["path"])

    run_reflex(r["id"], db_path=test_db["path"])
    run_reflex(r["id"], db_path=test_db["path"])

    # break it → one failure recorded (streak 1, still approved)
    with get_connection(test_db["path"]) as c:
        c.execute("UPDATE reflexes SET script = 'exit 3', approved_hash = ? WHERE id = ?",
                  (__import__("hashlib").sha256(b"exit 3").hexdigest(), r["id"]))
    run_reflex(r["id"], db_path=test_db["path"])

    rates = get_reflex_success_rates(db_path=test_db["path"])
    st = rates[r["id"]]
    assert st["runs"] == 3
    assert st["ok"] == 2
    assert st["rate"] == round(2 / 3, 3)
    assert st["avg_ms"] >= 0


def test_approve_refuses_broken_script(test_db):
    from src.database import get_connection

    conn = test_db["conn"]
    sid = _seed_skill(conn, name="Broken Script")
    conn.commit()
    with patch("src.llm.is_llm_available", return_value=False):
        r = promote_skill(sid, db_path=test_db["path"])
    with get_connection(test_db["path"]) as c:
        c.execute("UPDATE reflexes SET script = ? WHERE id = ?", ('if [ then fi (', r["id"]))

    with pytest.raises(ValueError, match="does not parse"):
        approve_reflex(r["id"], db_path=test_db["path"])


def test_template_draft_parses_and_reports_syntax_ok(test_db):
    conn = test_db["conn"]
    sid = _seed_skill(conn, name="Template Syntax")
    conn.commit()
    with patch("src.llm.is_llm_available", return_value=False):
        r = promote_skill(sid, db_path=test_db["path"])
    assert r["syntax_ok"] is True
    assert "set -euo pipefail" in r["script"]


def test_llm_draft_stores_inferred_params_schema(test_db):
    import json as _json

    conn = test_db["conn"]
    sid = _seed_skill(conn, name="Param Inference")
    conn.commit()

    llm_reply = _json.dumps({
        "script": "set -euo pipefail\necho \"$PARAM_SERVICE\"",
        "params": [{"name": "service", "description": "target service", "required": True}],
    })
    with patch("src.llm.is_llm_available", return_value=True), \
         patch("src.llm.call_chat_completion", return_value=llm_reply):
        r = promote_skill(sid, db_path=test_db["path"])

    assert r["drafted_by"] == "llm"
    rows = list_reflexes(db_path=test_db["path"])
    schema = _json.loads([x for x in rows if x["id"] == r["id"]][0]["params_schema"])
    assert "service" in schema["properties"]
    assert schema["required"] == ["service"]


def test_llm_draft_prompt_includes_related_mistake_guards(test_db):
    from src.memory_ops import create_mistake

    conn = test_db["conn"]
    sid = _seed_skill(conn, name="Guarded Deploy")
    create_mistake(
        conn,
        date="2026-07-01",
        context="guarded deploy",
        mistake="Deployed without saving env first",
        fix="save then deploy",
    )
    conn.commit()

    captured = {}

    def fake_chat(messages, task=None):
        captured["prompt"] = messages[0]["content"]
        return '{"script": "set -euo pipefail\\necho ok"}'

    with patch("src.llm.is_llm_available", return_value=True), \
         patch("src.llm.call_chat_completion", side_effect=fake_chat):
        promote_skill(sid, db_path=test_db["path"])

    assert "Known related failures" in captured["prompt"]
    assert "set -euo pipefail" in captured["prompt"]
