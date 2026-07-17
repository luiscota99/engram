"""Tests for retrieval feedback: recording, batch totals, the ranking effect,
the record_usage(success=False) fix, self-check proposals, and both surfaces.

Two invariants under test throughout: feedback affects RANKING only (never
existence), and dormancy (zero feedback, zero use) is never penalized or
proposed for deletion — non-use is not a signal.
"""

from __future__ import annotations

import io
import sys
from types import SimpleNamespace

import pytest

from src import feedback


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "mem.db"
    monkeypatch.setenv("ENGRAM_DB_PATH", str(path))
    monkeypatch.delenv("ENGRAM_AUDIT_LOG", raising=False)
    from src.database import init_db

    init_db(str(path))
    return str(path)


def _add_mistake(db_path, mistake, context="ctx"):
    from src.database import get_connection
    from src.memory_ops import create_mistake

    with get_connection(db_path) as conn:
        cur = create_mistake(
            conn, date="2026-07-17", context=context, mistake=mistake, fix="a fix"
        )
    return cur


# ── add_feedback ─────────────────────────────────────────────────────

def test_add_feedback_records_and_validates_item(db):
    _add_mistake(db, "vector norms mixed")
    assert feedback.add_feedback("mistake", 1, helpful=True, query="norms")
    assert feedback.add_feedback("mistake", 1, helpful=False)
    assert not feedback.add_feedback("mistake", 999, helpful=True)  # unknown id
    assert not feedback.add_feedback("nonsense", 1, helpful=True)  # unknown type
    totals = feedback.feedback_totals([("mistake", 1)])
    assert totals[("mistake", 1)] == (1, 1)


def test_feedback_totals_batches_all_types_one_query(db):
    _add_mistake(db, "m1")
    _add_mistake(db, "m2")
    feedback.add_feedback("mistake", 1, helpful=True)
    feedback.add_feedback("mistake", 2, helpful=False)
    feedback.add_feedback("mistake", 2, helpful=False)
    totals = feedback.feedback_totals([("mistake", 1), ("mistake", 2), ("skill", 7)])
    assert totals[("mistake", 1)] == (1, 0)
    assert totals[("mistake", 2)] == (0, 2)
    assert ("skill", 7) not in totals  # dormant: absent, not zero-penalized
    assert feedback.feedback_totals([]) == {}


# ── scoring stance ───────────────────────────────────────────────────

def test_unhelpful_outweighs_helped_and_dormant_is_neutral():
    assert feedback.feedback_score(0, 0) == 0.0  # dormancy costs nothing
    assert feedback.feedback_score(1, 0) > 0
    assert feedback.feedback_score(0, 1) < 0
    # precision over recall: one unhelpful outweighs one helped
    assert feedback.feedback_score(1, 1) < 0


def test_search_ranking_demotes_unhelpful_item(db):
    """Two near-identical memories; the one marked unhelpful must rank below."""
    _add_mistake(db, "ollama embedding timeout on cold start")
    _add_mistake(db, "ollama embedding timeout under load")
    from src.search import search

    def order():
        hits = search("ollama embedding timeout", limit=5, db_path=db)
        return [h["item_id"] for h in hits if h["item_type"] == "mistake"]

    baseline = order()
    demoted_id = baseline[0]
    for _ in range(3):
        feedback.add_feedback("mistake", demoted_id, helpful=False)
    reranked = order()
    assert reranked.index(demoted_id) > baseline.index(demoted_id)
    # ranking only — the item still exists and is still retrievable
    assert demoted_id in reranked


# ── record_usage success param now means something ───────────────────

def test_record_usage_failure_becomes_feedback_not_usage(db):
    _add_mistake(db, "m1")
    from src.database import get_connection
    from src.mcp.handlers import handle_memory_record_usage

    out = handle_memory_record_usage(
        {"item_type": "mistake", "item_id": 1, "success": False}
    )
    assert "demoted" in out
    with get_connection(db) as conn:
        row = conn.execute("SELECT usage_count FROM mistakes WHERE id = 1").fetchone()
    assert (row["usage_count"] or 0) == 0  # a failure must not boost
    assert feedback.feedback_totals([("mistake", 1)])[("mistake", 1)] == (0, 1)


# ── self-check proposals: user decides, dormancy never nominated ─────

def test_self_check_proposes_only_net_negative(db):
    from src.maintenance import run_self_check

    _add_mistake(db, "noisy memory")          # id 1 → net -2
    _add_mistake(db, "dormant memory")        # id 2 → no feedback at all
    _add_mistake(db, "one-off complaint")     # id 3 → net -1, below threshold
    feedback.add_feedback("mistake", 1, helpful=False)
    feedback.add_feedback("mistake", 1, helpful=False)
    feedback.add_feedback("mistake", 3, helpful=False)

    run_self_check(db_path=db)

    from src.database import get_connection

    with get_connection(db) as conn:
        keys = [
            r["finding_key"]
            for r in conn.execute(
                "SELECT finding_key FROM inbox WHERE finding_key LIKE 'feedback-negative:%'"
            ).fetchall()
        ]
    assert keys == ["feedback-negative:mistake:1"]  # dormant + one-off untouched
    # and it is a proposal — the item itself was NOT deleted or archived
    with get_connection(db) as conn:
        assert conn.execute("SELECT COUNT(*) c FROM mistakes").fetchone()["c"] == 3


# ── CLI and MCP surfaces ─────────────────────────────────────────────

def _capture(func, *args) -> str:
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        func(*args)
    finally:
        sys.stdout = old
    return buf.getvalue()


def test_cli_feedback_helped_and_unhelpful(db):
    from src.cli.commands.memory import cmd_feedback

    _add_mistake(db, "m1")
    out = _capture(
        cmd_feedback,
        SimpleNamespace(item="mistake:1", helped=True, unhelpful=False, query=None),
    )
    assert "rewarded" in out
    out = _capture(
        cmd_feedback,
        SimpleNamespace(item="mistake:1", helped=False, unhelpful=True, query="noise q"),
    )
    assert "demoted" in out
    assert feedback.feedback_totals([("mistake", 1)])[("mistake", 1)] == (1, 1)


def test_cli_feedback_rejects_ambiguous_flags(db):
    from src.cli.commands.memory import cmd_feedback

    _add_mistake(db, "m1")
    with pytest.raises(SystemExit):
        _capture(
            cmd_feedback,
            SimpleNamespace(item="mistake:1", helped=True, unhelpful=True, query=None),
        )


def test_mcp_memory_feedback(db):
    from src.mcp.handlers import handle_memory_feedback

    _add_mistake(db, "m1")
    assert "rewarded" in handle_memory_feedback(
        {"item_type": "mistake", "item_id": 1, "helpful": True}
    )
    assert "demoted" in handle_memory_feedback(
        {"item_type": "mistake", "item_id": 1, "helpful": False}
    )
    assert "Error" in handle_memory_feedback({"item_type": "mistake", "item_id": 1})
    assert "Error" in handle_memory_feedback(
        {"item_type": "mistake", "item_id": 999, "helpful": True}
    )
