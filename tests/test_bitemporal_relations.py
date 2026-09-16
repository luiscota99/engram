"""Bi-temporal + provenance edges (schema v27): event-time + ingestion-time
axes, code-set provenance/actor (anti-poisoning), functional supersede-on-change
via an explicit opt-in, and race-safe CAS writes."""

from __future__ import annotations

import pytest

from src import relations
from src.database import get_connection, init_db
from src.memory_ops import create_mistake, create_pattern, create_skill


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = str(tmp_path / "mem.db")
    monkeypatch.setenv("ENGRAM_DB_PATH", path)
    monkeypatch.delenv("ENGRAM_AUDIT_LOG", raising=False)
    init_db(path)
    with get_connection(path) as conn:
        create_mistake(conn, date="2026-07-21", context="c", mistake="m1", fix="f1")
        create_pattern(conn, name="P1", symptoms="s", root_cause="r", standard_fix="f")
        create_pattern(conn, name="P2", symptoms="s", root_cause="r", standard_fix="f")
        create_pattern(conn, name="P3", symptoms="s", root_cause="r", standard_fix="f")
        create_skill(conn, name="S1", domain="d", trigger="t", workflow="w")
    return path


# ── schema (fresh install carries the bi-temporal columns) ────────────

def test_fresh_db_has_bitemporal_columns(db):
    with get_connection(db) as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(memory_relations)").fetchall()}
    assert {"status", "actor", "provenance", "valid_from", "valid_to",
            "recorded_at", "invalidated_at"} <= cols


# ── provenance / actor (code-set) ─────────────────────────────────────

def test_provenance_and_actor_are_stored(db):
    assert relations.add_relation(
        "mistake", 1, "pattern", 1, "causes",
        actor="user:luis", provenance="manual", db_path=db,
    ) is None
    rel = relations.get_relations("mistake", 1, db_path=db)[0]
    assert rel["actor"] == "user:luis"
    assert rel["provenance"] == "manual"
    assert rel["status"] == "active"


def test_bad_provenance_rejected(db):
    err = relations.add_relation("mistake", 1, "pattern", 1, "causes",
                                 provenance="totally-made-up", db_path=db)
    assert err is not None and "provenance" in err


# ── functional supersede-on-change ───────────────────────────────────

def test_supersedes_keeps_multiple_losers(db):
    # Regression: supersedes is one-to-MANY (consolidation merges a cluster —
    # one keeper supersedes every loser). Asserting a second must NOT retire the
    # first, or the merge's provenance for all-but-the-last loser is lost.
    relations.add_relation("skill", 1, "pattern", 1, "supersedes", db_path=db)
    relations.add_relation("skill", 1, "pattern", 2, "supersedes", db_path=db)
    relations.add_relation("skill", 1, "pattern", 3, "supersedes", db_path=db)

    active = relations.get_relations("skill", 1, db_path=db)
    assert sorted(r["other_id"] for r in active) == [1, 2, 3]


def test_functional_flag_retires_prior_edge(db):
    # The replace-on-change mechanism still works when a caller opts in per call.
    relations.add_relation("skill", 1, "pattern", 1, "refines", functional=True, db_path=db)
    relations.add_relation("skill", 1, "pattern", 2, "refines", functional=True, db_path=db)

    active = relations.get_relations("skill", 1, db_path=db)
    assert len(active) == 1
    assert active[0]["other_id"] == 2  # newest wins

    allrels = relations.get_relations("skill", 1, db_path=db, include_invalidated=True)
    assert len(allrels) == 2
    retired = [r for r in allrels if r["status"] == "invalidated"][0]
    assert retired["other_id"] == 1
    assert retired["invalidated_at"] is not None and retired["valid_to"] is not None


def test_nonfunctional_relation_keeps_multiple(db):
    relations.add_relation("mistake", 1, "pattern", 1, "related", db_path=db)
    relations.add_relation("mistake", 1, "pattern", 2, "related", db_path=db)
    assert len(relations.get_relations("mistake", 1, db_path=db)) == 2


# ── invalidate + CAS re-assert ───────────────────────────────────────

def test_invalidate_then_reassert_reactivates_same_row(db):
    relations.add_relation("mistake", 1, "pattern", 1, "related", db_path=db)
    assert relations.invalidate_relation("mistake", 1, "pattern", 1, "related", db_path=db) is True
    assert relations.get_relations("mistake", 1, db_path=db) == []  # none active

    # Re-assert the identical edge: CAS upsert re-activates, no duplicate row.
    relations.add_relation("mistake", 1, "pattern", 1, "related", db_path=db)
    active = relations.get_relations("mistake", 1, db_path=db)
    assert len(active) == 1 and active[0]["status"] == "active"
    with get_connection(db) as conn:
        n = conn.execute("SELECT COUNT(*) FROM memory_relations").fetchone()[0]
    assert n == 1  # one physical row, re-activated (no history duplication)


# ── as_of point query (what was true when) ───────────────────────────

def test_as_of_point_query(db):
    # A fact known+true from Jan, retired end of Feb. Pin all timestamps for a
    # deterministic bi-temporal window (recorded_at must be <= as_of too).
    relations.add_relation("mistake", 1, "pattern", 1, "related", db_path=db)
    with get_connection(db) as conn:
        conn.execute(
            "UPDATE memory_relations SET status='invalidated', "
            "recorded_at='2026-01-01T00:00:00', valid_from='2026-01-01T00:00:00', "
            "valid_to='2026-03-01T00:00:00', invalidated_at='2026-03-01T00:00:00' "
            "WHERE from_id=1 AND to_id=1 AND relation='related'"
        )

    inside = relations.get_relations("mistake", 1, db_path=db, as_of="2026-02-01T00:00:00")
    assert len(inside) == 1  # known by then AND event-window covers Feb
    after = relations.get_relations("mistake", 1, db_path=db, as_of="2026-06-01T00:00:00")
    assert after == []  # past valid_to / invalidated_at
    before = relations.get_relations("mistake", 1, db_path=db, as_of="2025-12-01T00:00:00")
    assert before == []  # not yet recorded / not yet valid
