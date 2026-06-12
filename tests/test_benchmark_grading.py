"""Tests for benchmarks/grading.py abstention and recall logic."""

from __future__ import annotations

from benchmarks.grading import (
    abstention_success,
    query_term_overlap,
    recall_at_k,
)


def test_query_term_overlap_full_match():
    assert query_term_overlap("sqlite wal mode", {"title": "SQLite WAL", "snippet": "enable wal mode"}) == 1.0


def test_query_term_overlap_none():
    assert query_term_overlap("pasta carbonara recipe", {"title": "API Debugging", "snippet": "loop roles"}) == 0.0


def test_abstention_passes_on_low_overlap():
    q = {"expect_abstention": True, "query": "best pasta carbonara recipe"}
    results = [
        {"item_type": "skill", "item_id": 1, "title": "Debugging", "snippet": "workflow", "utility_score": 65.0},
    ]
    assert abstention_success(results, q, k=5) is True


def test_abstention_fails_on_high_overlap():
    q = {"expect_abstention": True, "query": "sqlite wal journal mode"}
    results = [
        {"item_type": "pattern", "item_id": 1, "title": "SQLite WAL", "snippet": "journal mode fix"},
    ]
    assert abstention_success(results, q, k=5) is False


def test_abstention_empty_results_passes():
    q = {"expect_abstention": True, "query": "off topic"}
    assert abstention_success([], q) is True


def test_recall_at_k_abstention_uses_overlap_not_utility():
    q = {"expect_abstention": True, "query": "NBA finals score"}
    results = [{"title": "Unrelated", "snippet": "engineering", "utility_score": 99.0}]
    assert recall_at_k(results, q, 5) == 1.0
