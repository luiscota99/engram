"""Tests for reciprocal rank fusion (RRF) and FTS query helpers."""
from __future__ import annotations

import pytest

from src.ranking import reciprocal_rank_scores, result_key


def mk_row(item_type: str, item_id: int) -> dict:
    return {"item_type": item_type, "item_id": item_id}


def test_result_key_format():
    assert result_key({"item_type": "skill", "item_id": 7}) == "skill-7"


def test_rrf_two_lists_ordering_normalized():
    # semantic order: skill-1 rank1, mistake-2 rank2
    # lexical order: mistake-2 rank1, skill-1 rank2 → mistake stronger in lexical
    sem = [mk_row("skill", 1), mk_row("mistake", 2)]
    lex = [mk_row("mistake", 2), mk_row("skill", 1)]
    scores = reciprocal_rank_scores(sem, lex)
    assert "skill-1" in scores and "mistake-2" in scores
    # Both appear in both lists — symmetric fusion should tie exactly.
    assert scores["skill-1"] == pytest.approx(scores["mistake-2"], rel=1e-9)
    assert max(scores.values()) <= 1.0 + 1e-9
    assert min(scores.values()) >= 0.0


def test_rrf_semantic_only_weights_top():
    sem = [mk_row("pattern", 1), mk_row("skill", 2)]
    scores = reciprocal_rank_scores(sem, [])
    # rank 1 dominates rank 2
    assert scores["pattern-1"] > scores["skill-2"]


def test_rrf_lexical_only():
    lex = [mk_row("mistake", 9)]
    scores = reciprocal_rank_scores([], lex)
    assert scores["mistake-9"] == pytest.approx(1.0)


def test_rrf_both_empty():
    assert reciprocal_rank_scores([], []) == {}


def test_rrf_semantic_only_single_doc_normalizes_to_one():
    """Single hit in semantic-only list maps to fused score 1.0 after normalization."""
    sem = [mk_row("skill", 42)]
    assert reciprocal_rank_scores(sem, [])["skill-42"] == pytest.approx(1.0)


def test_fts_query_terms_strips_trailing_dot():
    from src.search import _fts_query_terms

    assert "migration" in _fts_query_terms("database Migration.")
    assert _fts_query_terms("foo bar") == ["foo", "bar"]
