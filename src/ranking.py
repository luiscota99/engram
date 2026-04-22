"""
Ranking module — multi-factor utility scoring for search results.

Replaces the simple linear scoring (base + usage*15) with a system that
accounts for recency decay, logarithmic usage boost, project affinity,
item-type relevance, and embedding staleness.
"""

from __future__ import annotations


import math
from datetime import datetime, timezone


# Half-life in days for recency decay.
# After 90 days of no use, a memory's recency factor is 0.5.
RECENCY_HALF_LIFE_DAYS = 90

# Penalty applied when an embedding was generated with a stale model.
STALE_EMBEDDING_PENALTY = 10.0

# Base scores by search method
BASE_SCORE_SEMANTIC = 100.0
BASE_SCORE_LEXICAL = 50.0

# Project affinity boost values
AFFINITY_BOOSTS = {
    "created": 40.0,
    "used": 25.0,
    "relevant": 10.0,
}

# Boost applied when the query explicitly targets this item type
TYPE_MATCH_BOOST = 20.0


def _recency_factor(last_used_at: str | None) -> float:
    """Return a 0.0–1.0 decay multiplier based on last_used_at.

    Uses exponential decay with RECENCY_HALF_LIFE_DAYS half-life.
    Items never used default to a factor of 0.5 (neutral).
    """
    if not last_used_at:
        return 0.5
    try:
        last = datetime.fromisoformat(last_used_at)
        # Make offset-naive for comparison if needed
        if last.tzinfo is not None:
            now = datetime.now(timezone.utc)
        else:
            now = datetime.now()
        days_old = max(0, (now - last).days)
        return math.pow(0.5, days_old / RECENCY_HALF_LIFE_DAYS)
    except (ValueError, TypeError):
        return 0.5


def _usage_boost(usage_count: int) -> float:
    """Logarithmic usage boost to prevent high-use items from drowning others.

    usage_count=0  →  0.0
    usage_count=1  →  10.0
    usage_count=10 →  23.0
    usage_count=100 → 46.1
    """
    return 10.0 * math.log1p(max(0, usage_count))


def calculate_utility_score(
    result: dict,
    usage_count: int = 0,
    last_used_at: str | None = None,
    affinity: str | None = None,
    inferred_type: str | None = None,
    embedding_is_stale: bool = False,
) -> float:
    """Compute a composite utility score for a single search result.

    Parameters
    ----------
    result:
        The raw search result dict (must contain 'item_type' and 'is_semantic').
    usage_count:
        How many times this item has been retrieved and used.
    last_used_at:
        ISO datetime string of last usage.
    affinity:
        Project affinity: 'created' | 'used' | 'relevant' | None.
    inferred_type:
        Item type inferred from the query (e.g. 'mistake').  When it matches
        result['item_type'] the result gets a small boost.
    embedding_is_stale:
        True when the stored embedding was generated with an older model.
    """
    base = BASE_SCORE_SEMANTIC if result.get("is_semantic") else BASE_SCORE_LEXICAL

    # Apply recency decay to base score
    decayed_base = base * _recency_factor(last_used_at)

    # Log-scale usage boost (doesn't decay — demonstrated value persists)
    usage = _usage_boost(usage_count)

    # Project affinity boost
    affinity_boost = AFFINITY_BOOSTS.get(affinity or "", 0.0)

    # Type-match boost
    type_boost = TYPE_MATCH_BOOST if (
        inferred_type and inferred_type == result.get("item_type")
    ) else 0.0

    # Stale embedding penalty
    stale_penalty = STALE_EMBEDDING_PENALTY if embedding_is_stale else 0.0

    return decayed_base + usage + affinity_boost + type_boost - stale_penalty


def infer_type_from_query(query: str) -> str | None:
    """Heuristically detect if a query is asking for a specific memory type.

    Returns the item_type string if detected, else None.
    """
    query_lower = query.lower()
    type_keywords = {
        "mistake": ["mistake", "error", "bug", "wrong", "broke", "failed", "problem"],
        "pattern": ["pattern", "recurring", "keep seeing", "always happens", "anti-pattern"],
        "skill": ["skill", "workflow", "how to", "steps to", "process for", "procedure"],
        "conversation": ["conversation", "session", "discussed", "talked about", "decided"],
        "prompt": ["prompt", "system prompt", "instruction", "persona"],
    }
    for item_type, keywords in type_keywords.items():
        if any(kw in query_lower for kw in keywords):
            return item_type
    return None


def rank_results(
    results: list[dict],
    usage_counts: dict,
    last_used_map: dict,
    affinities: dict,
    query: str = "",
    stale_rowids: set | None = None,
) -> list[dict]:
    """Apply utility scoring to a list of results and sort descending.

    Parameters
    ----------
    results:
        List of raw search result dicts.
    usage_counts:
        Dict mapping (item_type, item_id_int) → usage_count.
    last_used_map:
        Dict mapping (item_type, item_id_int) → last_used_at string.
    affinities:
        Dict mapping (item_type, item_id_int) → affinity string.
    query:
        Original search query string (used for type inference).
    stale_rowids:
        Set of FTS rowids whose embeddings are stale.
    """
    inferred_type = infer_type_from_query(query)
    stale_rowids = stale_rowids or set()

    for r in results:
        key = (r["item_type"], int(r["item_id"]))
        score = calculate_utility_score(
            result=r,
            usage_count=usage_counts.get(key, 0),
            last_used_at=last_used_map.get(key),
            affinity=affinities.get(key),
            inferred_type=inferred_type,
            embedding_is_stale=r.get("rowid") in stale_rowids,
        )
        r["utility_score"] = score

    results.sort(key=lambda x: x.get("utility_score", 0.0), reverse=True)
    return results
