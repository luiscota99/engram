"""Tests for the action-ladder router: reflex → recall → reason."""
from __future__ import annotations

from unittest.mock import patch

from src.database import index_in_fts
from src.memory_ops import create_mistake, create_skill
from src.reflex import approve_reflex, promote_skill
from src.router import route_task


def _seed_skill(conn, name="Deploy Rollback", workflow="1. stop 2. revert 3. verify"):
    sid = create_skill(
        conn,
        name=name,
        domain="ops",
        trigger="deploy went bad",
        workflow=workflow,
    )
    return sid


def test_reason_rung_on_empty_db(test_db):
    result = route_task("configure quantum flux capacitor", db_path=test_db["path"])
    assert result["rung"] == "reason"
    assert "NO PRIOR ART" in result["text"]
    assert "capture" in result["text"].lower()


def test_recall_rung_when_skill_matches(test_db):
    conn = test_db["conn"]
    _seed_skill(conn)
    conn.commit()

    result = route_task("deploy rollback procedure", db_path=test_db["path"])
    assert result["rung"] == "recall"
    assert "PRIOR ART FOUND" in result["text"]
    assert any(m["item_type"] == "skill" for m in result["matches"])


def test_reflex_rung_beats_recall(test_db):
    conn = test_db["conn"]
    sid = _seed_skill(conn)
    conn.commit()
    with patch("src.llm.is_llm_available", return_value=False):
        r = promote_skill(sid, db_path=test_db["path"])
    from src.database import get_connection

    with get_connection(test_db["path"]) as c:
        c.execute("UPDATE reflexes SET script = 'echo ok' WHERE id = ?", (r["id"],))
    approve_reflex(r["id"], db_path=test_db["path"])

    result = route_task("deploy rollback procedure", db_path=test_db["path"])
    assert result["rung"] == "reflex"
    assert "reflex_deploy_rollback" in result["text"]
    assert "do not re-derive" in result["text"].lower()


def test_unapproved_reflex_does_not_route(test_db):
    conn = test_db["conn"]
    sid = _seed_skill(conn)
    conn.commit()
    with patch("src.llm.is_llm_available", return_value=False):
        promote_skill(sid, db_path=test_db["path"])  # drafted, NOT approved

    result = route_task("deploy rollback procedure", db_path=test_db["path"])
    assert result["rung"] == "recall"  # falls through to prior art


def test_mistake_warnings_surface_on_every_rung(test_db):
    conn = test_db["conn"]
    create_mistake(
        conn,
        date="2026-07-01",
        context="deploy rollback",
        mistake="Rolled back deploy without stopping workers first",
        fix="stop workers before revert",
    )
    conn.commit()

    result = route_task("deploy rollback without stopping workers", db_path=test_db["path"])
    assert result["warnings"], "matching mistakes must surface as warnings"
    assert "Known pitfalls" in result["text"]


def test_route_output_is_token_lean(test_db):
    conn = test_db["conn"]
    _seed_skill(conn, workflow="x" * 5000)  # bloated workflow must not leak through
    index_in_fts(conn, "skill", 1, "Deploy Rollback", "x" * 5000, [])
    conn.commit()

    result = route_task("deploy rollback procedure", db_path=test_db["path"])
    assert len(result["text"]) < 1200, "route output must stay within ~300 tokens"
