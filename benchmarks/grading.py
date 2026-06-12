"""Shared grading logic for retrieval benchmarks (title substring vs item id + type)."""

from __future__ import annotations

import math
from typing import Any


def normalize_item_id(val: Any) -> int:
    """Coerce FTS item_id (often str) to int for comparison."""
    if val is None:
        return -1
    return int(val)


def use_id_grading(q: dict) -> bool:
    """Prefer (expected_type, expected_item_id) when both are set."""
    return q.get("expected_item_id") is not None and bool(q.get("expected_type"))


def expects_abstention(q: dict) -> bool:
    """Query should retrieve no relevant hit (negative / abstention label)."""
    return bool(q.get("expect_abstention"))


def row_matches_expected(r: dict, q: dict) -> bool:
    """Return True if this result row is the labeled correct hit."""
    if use_id_grading(q):
        return (
            r.get("item_type") == q["expected_type"]
            and normalize_item_id(r.get("item_id")) == normalize_item_id(q["expected_item_id"])
        )
    needle = (q.get("expected_title_contains") or "").lower()
    title = (r.get("title") or "").lower()
    return bool(needle) and needle in title


def relevances_from_results(results: list[dict], q: dict) -> list[float]:
    """Binary relevance (0/1) per rank position for the full result list."""
    if expects_abstention(q):
        return [0.0 for _ in results]
    return [1.0 if row_matches_expected(r, q) else 0.0 for r in results]


def abstention_success(results: list[dict], q: dict, k: int = 5) -> bool:
    """Abstention passes when top-k is empty or all hits score below threshold."""
    if not expects_abstention(q):
        return False
    top = results[:k]
    if not top:
        return True
    deny = q.get("deny_items") or []
    for r in top:
        for d in deny:
            if (
                r.get("item_type") == d.get("type")
                and normalize_item_id(r.get("item_id")) == normalize_item_id(d.get("item_id"))
            ):
                return False
    threshold = float(q.get("abstention_max_score", 0.35))
    return all(float(r.get("utility_score", 1.0)) < threshold for r in top)


def dcg(relevances: list[float], k: int) -> float:
    score = 0.0
    for i, rel in enumerate(relevances[:k]):
        score += rel / math.log2(i + 2)
    return score


def ndcg_at_k_from_relevances(relevances: list[float], k: int) -> float:
    if not relevances:
        return 0.0
    ideal = sorted(relevances, reverse=True)
    idcg = dcg(ideal, k)
    if idcg == 0:
        return 0.0
    return dcg(relevances, k) / idcg


def recall_at_k_from_relevances(relevances: list[float], k: int) -> float:
    return 1.0 if any(relevances[:k]) else 0.0


def recall_at_k(results: list[dict], q: dict, k: int) -> float:
    """Recall@k with abstention-aware scoring."""
    if expects_abstention(q):
        return 1.0 if abstention_success(results, q, k) else 0.0
    rel = relevances_from_results(results, q)
    return recall_at_k_from_relevances(rel, k)


def mrr_from_relevances(relevances: list[float]) -> float:
    for i, rel in enumerate(relevances, start=1):
        if rel >= 1.0:
            return 1.0 / i
    return 0.0
