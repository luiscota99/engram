"""
Ranking module — multi-factor utility scoring for search results.

Replaces the simple linear scoring (base + usage*15) with a system that
accounts for recency decay, logarithmic usage boost, project affinity,
item-type relevance, embedding staleness, auto-detected tag matches,
and BM25 reranking of semantic candidates.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from datetime import datetime, timezone

# Half-life in days for recency decay.
# After 90 days of no use, a memory's recency factor is 0.5.
RECENCY_HALF_LIFE_DAYS = 90

# Recency scales only this fraction of the base score. Previously the raw
# factor multiplied the WHOLE base, so a correct-but-cold memory started at
# half its relevance — the antithesis of recall. Now: base * (floor + span*f).
RECENCY_FLOOR = 0.75
RECENCY_SPAN = 0.25

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
TYPE_MATCH_BOOST = 15.0

# Multiplicative boost when query intent matches result type
INTENT_TYPE_MULTIPLIERS: dict[str, float] = {
    "mistake": 1.25,
    "pattern": 1.30,
    "skill": 1.20,
    "conversation": 1.15,
    "prompt": 1.35,
}

# Boost applied per matched auto-detected tag
TAG_MATCH_BOOST = 15.0

# BM25 parameters
BM25_K1 = 1.5   # term frequency saturation
BM25_B = 0.75   # document length normalization
BM25_WEIGHT = 0.3  # how much BM25 adjusts the final score: score *= (1 + BM25_WEIGHT * bm25)

# Reciprocal rank fusion — combines semantic vs lexical ranked lists (see search.py).
# Typical k≈60 (Cormack-style RRF). Normalized scores are summed into utility_score.
RRF_K = 60
RRF_WEIGHT = 50.0  # was 15 — cosmetic next to base 100; fusion must be able to reorder


def result_key(result: dict) -> str:
    """Stable key for a search result row: ``"{item_type}-{item_id}"``."""
    return f"{result['item_type']}-{result['item_id']}"


def reciprocal_rank_scores(
    semantic: list[dict],
    lexical: list[dict],
    *,
    k: int = RRF_K,
) -> dict[str, float]:
    """Reciprocal rank fusion scores per result key, normalized to ``[0, 1]``.

    For rank ``r`` (1-based) in each list, contributes ``1/(k + r)`` to that
    document. Normalization divides by ``max(score)`` so the strongest fused
    hit maps to ``1.0``.
    """
    raw: dict[str, float] = {}
    for rank, r in enumerate(semantic, start=1):
        key = result_key(r)
        raw[key] = raw.get(key, 0.0) + 1.0 / (k + rank)
    for rank, r in enumerate(lexical, start=1):
        key = result_key(r)
        raw[key] = raw.get(key, 0.0) + 1.0 / (k + rank)
    if not raw:
        return {}
    m = max(raw.values())
    if m <= 0:
        return {kk: 0.0 for kk in raw}
    return {kk: v / m for kk, v in raw.items()}


# ── BM25 ─────────────────────────────────────────────────────────────────────

def _tokenize(text: str) -> list[str]:
    """Lowercase, split on non-alphanumeric boundaries."""
    return re.findall(r"[a-z0-9]+", text.lower())


def bm25_score(query: str, document: str, avg_doc_len: float, doc_count: int,
               df: dict[str, int]) -> float:
    """Compute BM25 score for a single (query, document) pair.

    Parameters
    ----------
    query:
        The search query string.
    document:
        The full text of the document (title + snippet + tags concatenated).
    avg_doc_len:
        Average document length across the corpus (in tokens).
    doc_count:
        Total number of documents in the corpus.
    df:
        Document frequency dict: term → number of docs containing that term.
        If empty, IDF falls back to 1.0 for all terms.
    """
    query_terms = _tokenize(query)
    if not query_terms:
        return 0.0

    doc_tokens = _tokenize(document)
    doc_len = len(doc_tokens)
    tf_map = Counter(doc_tokens)

    score = 0.0
    for term in query_terms:
        tf = tf_map.get(term, 0)
        if tf == 0:
            continue

        # IDF with smoothing
        n_docs_with_term = df.get(term, 0)
        idf = math.log((doc_count - n_docs_with_term + 0.5) / (n_docs_with_term + 0.5) + 1)

        # Normalised TF
        norm_tf = (tf * (BM25_K1 + 1)) / (
            tf + BM25_K1 * (1 - BM25_B + BM25_B * doc_len / max(avg_doc_len, 1))
        )
        score += idf * norm_tf

    return score


def bm25_scores(query: str, results: list[dict]) -> dict[str, float]:
    """Compute BM25 scores for a list of search result dicts.

    Each result must have at least a ``title`` key; ``snippet`` and ``tags``
    are used when present.

    Returns a dict mapping ``"{item_type}-{item_id}"`` → normalised BM25 score
    (0.0–1.0).  Scores are normalised so the best result gets 1.0.
    """
    if not results or not query.strip():
        return {}

    # Build the corpus for IDF computation
    documents: list[tuple[str, str]] = []
    for r in results:
        text = " ".join(filter(None, [r.get("title", ""), r.get("snippet", ""), r.get("tags", "")]))
        key = f"{r['item_type']}-{r['item_id']}"
        documents.append((key, text))

    doc_count = len(documents)
    avg_doc_len = sum(len(_tokenize(text)) for _, text in documents) / max(doc_count, 1)

    # Document frequency
    df: dict[str, int] = Counter()
    for _, text in documents:
        for term in set(_tokenize(text)):
            df[term] += 1

    raw_scores: dict[str, float] = {}
    for key, text in documents:
        raw_scores[key] = bm25_score(query, text, avg_doc_len, doc_count, df)

    # Normalise to [0, 1]
    max_score = max(raw_scores.values()) if raw_scores else 0.0
    if max_score <= 0:
        return {k: 0.0 for k in raw_scores}
    return {k: v / max_score for k, v in raw_scores.items()}


def rerank_with_bm25(results: list[dict], query: str) -> list[dict]:
    """Adjust ``utility_score`` of each result using BM25 as a reranking signal.

    Formula: ``final_score = utility_score * (1 + BM25_WEIGHT * normalised_bm25)``

    This means:
    - A result with the highest BM25 score gets a BM25_WEIGHT (30%) boost.
    - A result with zero BM25 overlap is unaffected (multiplied by 1.0).
    - Utility score remains the dominant factor; BM25 only reorders ties.
    """
    if not results or not query.strip():
        return results

    scores = bm25_scores(query, results)
    for r in results:
        key = f"{r['item_type']}-{r['item_id']}"
        b25 = scores.get(key, 0.0)
        r["bm25_score"] = round(b25, 4)
        r["utility_score"] = r.get("utility_score", 0.0) * (1 + BM25_WEIGHT * b25)

    results.sort(key=lambda x: x.get("utility_score", 0.0), reverse=True)
    return results


# ─────────────────────────────────────────────────────────────────────────────

def _recency_factor(last_used_at: str | None, stability: float | None = None) -> float:
    """Return a 0.0–1.0 decay multiplier based on last_used_at.

    Items WITH a forgetting-curve stability (earned through usage/feedback
    events, schema v25) decay on their personal FSRS power-law curve — a
    memory that keeps proving itself earns a months-long curve, one that
    lapsed decays fast. Items without one keep the original fixed
    RECENCY_HALF_LIFE_DAYS exponential, so cold memories and seeded
    benchmark DBs score exactly as before. Items never used default to 0.5
    (neutral).
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
        if stability is not None and stability > 0:
            from .stability import retrievability

            return retrievability(days_old, stability)
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


def _tag_boost(result_tags_str: str | None, detected_tags: list[str]) -> float:
    """Return additive boost for every detected tag that appears in the result's tags."""
    if not detected_tags or not result_tags_str:
        return 0.0
    result_tags_lower = result_tags_str.lower()
    matches = sum(1 for t in detected_tags if t.lower() in result_tags_lower)
    return TAG_MATCH_BOOST * matches


def calculate_utility_score(
    result: dict,
    usage_count: int = 0,
    last_used_at: str | None = None,
    affinity: str | None = None,
    inferred_type: str | None = None,
    embedding_is_stale: bool = False,
    detected_tags: list[str] | None = None,
    stability: float | None = None,
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
    detected_tags:
        Auto-detected technology tags from the query.  Each tag that matches
        the result's stored tags adds TAG_MATCH_BOOST to the score.
    """
    base = BASE_SCORE_SEMANTIC if result.get("is_semantic") else BASE_SCORE_LEXICAL

    # Recency nudges relevance, never dominates it (floor at RECENCY_FLOOR)
    decayed_base = base * (
        RECENCY_FLOOR + RECENCY_SPAN * _recency_factor(last_used_at, stability)
    )

    # Log-scale usage boost (doesn't decay — demonstrated value persists)
    usage = _usage_boost(usage_count)

    # Project affinity boost
    affinity_boost = AFFINITY_BOOSTS.get(affinity or "", 0.0)

    # Type-match boost (additive) + intent multiplier for category-aware retrieval
    type_boost = TYPE_MATCH_BOOST if (
        inferred_type and inferred_type == result.get("item_type")
    ) else 0.0

    # Auto-detected tag match boost
    tag_boost = _tag_boost(result.get("tags"), detected_tags or [])

    # Stale embedding penalty
    stale_penalty = STALE_EMBEDDING_PENALTY if embedding_is_stale else 0.0

    score = decayed_base + usage + affinity_boost + type_boost + tag_boost - stale_penalty
    if inferred_type and inferred_type == result.get("item_type"):
        score *= INTENT_TYPE_MULTIPLIERS.get(inferred_type, 1.0)

    # Per-type relevance weight from the registry (mistakes/skills over
    # conversations/sessions). Defined since the registry existed but never
    # applied — wiring it keeps low-signal types from crowding the top-k.
    from .item_registry import rank_multiplier_for

    score *= rank_multiplier_for(result.get("item_type"))
    return score


def _query_implies_ide_or_rules_prompt(query_lower: str) -> bool:
    """High-precision cues: Cursor/IDE rules files before generic 'how to' → skill."""
    if any(
        s in query_lower
        for s in (
            ".mdc",
            ".cursorrules",
            ".cursor/rules",
            "/.cursor/",
            "cursor rules",
            "cursor rule",
        )
    ):
        return True
    if "cursorrules" in query_lower:
        return True
    # Standalone "mdc" as a token (not a substring of another word)
    if re.search(r"(?<![a-z0-9])mdc(?![a-z0-9])", query_lower):
        return True
    return False


def infer_type_from_query(query: str) -> str | None:
    """Heuristically detect if a query is asking for a specific memory type.

    Returns the item_type string if detected, else None.

    IDE/rules-file cues (``.mdc``, cursor rules, etc.) map to **prompt** before
    generic ``how to`` would map to **skill**, avoiding type-inference collisions.
    """
    query_lower = query.lower()
    if _query_implies_ide_or_rules_prompt(query_lower):
        return "prompt"

    # Order matters: mistake → pattern → skill → conversation → prompt
    type_keywords: list[tuple[str, list[str]]] = [
        ("mistake", ["mistake", "error", "bug", "wrong", "broke", "failed", "problem"]),
        ("pattern", ["pattern", "recurring", "keep seeing", "always happens", "anti-pattern"]),
        ("skill", ["skill", "workflow", "how to", "steps to", "process for", "procedure"]),
        ("conversation", ["conversation", "session", "discussed", "talked about", "decided"]),
        (
            "prompt",
            ["prompt", "system prompt", "instruction", "persona", "rules.mdc"],
        ),
    ]
    for item_type, keywords in type_keywords:
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
    detected_tags: list[str] | None = None,
    rrf_scores: dict[str, float] | None = None,
    item_dates: dict | None = None,
    temporal_intent: dict | None = None,
    feedback_map: dict | None = None,
    stability_by_key: dict | None = None,
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
    detected_tags:
        Auto-detected technology tags from the query (from query_analyzer).
    rrf_scores:
        Normalized reciprocal-rank fusion scores per ``result_key``, when hybrid
        search merged semantic + lexical lists.
    feedback_map:
        Dict mapping (item_type, item_id_int) → (helped, unhelpful) explicit
        feedback totals. Affects ranking only — an item with no feedback is
        merely dormant and scores exactly as before.
    stability_by_key:
        Dict mapping (item_type, item_id_int) → FSRS stability (days). Items
        present decay on their personal forgetting curve; absent items keep
        the fixed half-life.
    """
    inferred_type = infer_type_from_query(query)
    stale_rowids = stale_rowids or set()

    for r in results:
        key_row = (r["item_type"], int(r["item_id"]))
        base = calculate_utility_score(
            result=r,
            usage_count=usage_counts.get(key_row, 0),
            last_used_at=last_used_map.get(key_row),
            affinity=affinities.get(key_row),
            inferred_type=inferred_type,
            embedding_is_stale=r.get("rowid") in stale_rowids,
            detected_tags=detected_tags,
            stability=(stability_by_key or {}).get(key_row),
        )
        rk = result_key(r)
        rrf = RRF_WEIGHT * rrf_scores.get(rk, 0.0) if rrf_scores else 0.0
        r["rrf_normalized"] = round(rrf_scores.get(rk, 0.0), 6) if rrf_scores else 0.0
        fb = 0.0
        if feedback_map:
            helped, unhelpful = feedback_map.get(key_row, (0, 0))
            if helped or unhelpful:
                from .feedback import feedback_score

                fb = feedback_score(helped, unhelpful)
        r["utility_score"] = base + rrf + fb

    if temporal_intent and temporal_intent.get("has_temporal") and item_dates:
        _apply_temporal_boost(results, item_dates, temporal_intent)

    results.sort(key=lambda x: x.get("utility_score", 0.0), reverse=True)
    return results


# Multiplier when an item's date matches an explicit date in the query
# ("in May 2023"); modest so it re-orders near-ties, not everything.
# NOTE: a directional heuristic ("first"/"most recent" → prefer oldest/newest)
# was implemented and benchmarked on LongMemEval (2026-07-07): zero effect on
# R@5, slightly negative MRR — removed. Only explicit date matches remain.
TEMPORAL_DATE_MATCH_BOOST = 1.3


def _apply_temporal_boost(results: list[dict], item_dates: dict, intent: dict) -> None:
    """Adjust utility scores in place when the query mentions explicit dates.

    Matches by ISO-prefix: item date "2023-05-14" matches query prefix
    "2023-05" (from "in May 2023"). Inert for queries without date mentions.
    """
    dated = []
    for r in results:
        try:
            d = item_dates.get((r["item_type"], int(r["item_id"])))
        except (TypeError, ValueError):
            d = None
        if d:
            # normalize separators so "2023/05/20" matches prefix "2023-05"
            dated.append((r, str(d)[:10].replace("/", "-")))

    if not dated:
        return

    for r, d in dated:
        for prefix in intent.get("dates", []):
            if d.startswith(prefix):
                r["utility_score"] *= TEMPORAL_DATE_MATCH_BOOST
                r["temporal_boost"] = "date_match"
                break
