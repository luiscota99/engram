"""Tests for per-memory forgetting curves (FSRS-4.5, schema v25).

Invariants under test: the curve's fixed points, lapse-never-increases-
stability, event-driven state evolution, the ranking effect, and — most
importantly — that items with NO dynamics row score exactly as they did
before this feature existed (conservative integration; benchmark-stable).
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta

import pytest

from src import stability
from src.database import get_connection, init_db


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = str(tmp_path / "mem.db")
    monkeypatch.setenv("ENGRAM_DB_PATH", path)
    monkeypatch.delenv("ENGRAM_AUDIT_LOG", raising=False)
    init_db(path)
    return path


def _add_mistake(db_path, mistake):
    from src.memory_ops import create_mistake

    with get_connection(db_path) as conn:
        create_mistake(conn, date="2026-07-17", context="ctx", mistake=mistake, fix="f")


# ── curve invariants ─────────────────────────────────────────────────

def test_retrievability_fixed_points_and_monotonicity():
    s = 30.0
    assert stability.retrievability(0, s) == pytest.approx(1.0)
    assert stability.retrievability(s, s) == pytest.approx(0.9, abs=1e-9)
    samples = [stability.retrievability(t, s) for t in (0, 1, 10, 30, 90, 365)]
    assert samples == sorted(samples, reverse=True)


def test_init_stability_orders_by_rating():
    inits = [stability.init_stability(g) for g in (1, 2, 3, 4)]
    assert inits == sorted(inits)  # again < hard < good < easy


def test_recall_grows_and_lapse_never_grows():
    d, s = 5.0, 10.0
    r = stability.retrievability(5, s)
    grown = stability.next_stability_recall(d, s, r, stability.GOOD)
    assert grown > s
    # easy grows more than good
    assert stability.next_stability_recall(d, s, r, stability.EASY) > grown
    # lapse: for a wide range of states, post-lapse stability <= prior
    for s0 in (0.5, 3.0, 30.0, 300.0, 3000.0):
        for r0 in (0.99, 0.9, 0.5, 0.1):
            assert stability.next_stability_forget(5.0, s0, r0) <= s0


def test_growth_has_diminishing_returns():
    d, r = 5.0, 0.9
    small = stability.next_stability_recall(d, 5.0, r, stability.GOOD) / 5.0
    large = stability.next_stability_recall(d, 500.0, r, stability.GOOD) / 500.0
    assert large < small  # relative growth shrinks as s grows


# ── event-driven state ───────────────────────────────────────────────

def test_record_event_creates_then_evolves(db):
    _add_mistake(db, "m1")
    stability.record_event("mistake", 1, stability.GOOD, db_path=db)
    with get_connection(db) as c:
        row = dict(c.execute("SELECT * FROM memory_dynamics").fetchone())
    assert row["reps"] == 1 and row["lapses"] == 0
    s1 = row["stability"]
    assert s1 == pytest.approx(stability.init_stability(stability.GOOD))

    stability.record_event("mistake", 1, stability.EASY, db_path=db)
    stability.record_event("mistake", 1, stability.AGAIN, db_path=db)
    with get_connection(db) as c:
        row = dict(c.execute("SELECT * FROM memory_dynamics").fetchone())
    assert row["reps"] == 3 and row["lapses"] == 1


def test_usage_and_feedback_drive_dynamics(db):
    from src.database import record_usage
    from src.feedback import add_feedback

    _add_mistake(db, "m1")
    record_usage("mistake", 1, db_path=db)          # → good
    add_feedback("mistake", 1, helpful=True, db_path=db)   # → easy
    with get_connection(db) as c:
        after_growth = c.execute("SELECT stability, reps FROM memory_dynamics").fetchone()
    assert after_growth["reps"] == 2

    add_feedback("mistake", 1, helpful=False, db_path=db)  # → lapse
    with get_connection(db) as c:
        after_lapse = c.execute("SELECT stability, lapses FROM memory_dynamics").fetchone()
    assert after_lapse["lapses"] == 1
    assert after_lapse["stability"] <= after_growth["stability"]


def test_stability_map_batches_and_omits_dormant(db):
    _add_mistake(db, "m1")
    _add_mistake(db, "m2")
    stability.record_event("mistake", 1, stability.GOOD, db_path=db)
    m = stability.stability_map([("mistake", 1), ("mistake", 2)], db_path=db)
    assert ("mistake", 1) in m
    assert ("mistake", 2) not in m  # dormant: absent, not zeroed
    assert stability.stability_map([], db_path=db) == {}


# ── ranking behavior ─────────────────────────────────────────────────

def test_recency_factor_without_stability_is_unchanged():
    """Regression: no dynamics row → the ORIGINAL fixed-half-life curve."""
    from src.ranking import RECENCY_HALF_LIFE_DAYS, _recency_factor

    forty_days_ago = (datetime.now() - timedelta(days=40)).isoformat()
    expected = math.pow(0.5, 40 / RECENCY_HALF_LIFE_DAYS)
    assert _recency_factor(forty_days_ago, None) == pytest.approx(expected, abs=1e-6)
    assert _recency_factor(None, None) == 0.5


def test_recency_factor_with_stability_uses_personal_curve():
    from src.ranking import _recency_factor

    forty_days_ago = (datetime.now() - timedelta(days=40)).isoformat()
    stable = _recency_factor(forty_days_ago, 400.0)   # proven memory
    fragile = _recency_factor(forty_days_ago, 2.0)    # lapsed memory
    fixed = _recency_factor(forty_days_ago, None)
    assert stable > fixed > fragile
    assert stable == pytest.approx(stability.retrievability(40, 400.0))


def test_search_ranks_proven_memory_above_lapsed_twin(db):
    """Two near-identical old memories; the one with earned stability wins."""
    from src.search import search

    _add_mistake(db, "docker build cache invalidation surprise one")
    _add_mistake(db, "docker build cache invalidation surprise two")
    with get_connection(db) as c:
        # both last used 60 days ago — recency differentiates only via stability
        c.execute("UPDATE mistakes SET last_used_at = datetime('now', '-60 days')")
        # item 1 earned a long curve; item 2 lapsed to a fragile one
        c.execute(
            "INSERT INTO memory_dynamics (item_type, item_id, stability) VALUES ('mistake', 1, 500.0)"
        )
        c.execute(
            "INSERT INTO memory_dynamics (item_type, item_id, stability) VALUES ('mistake', 2, 0.5)"
        )
    hits = search("docker build cache invalidation", limit=5, db_path=db)
    ids = [int(h["item_id"]) for h in hits if h["item_type"] == "mistake"]
    assert ids.index(1) < ids.index(2)
