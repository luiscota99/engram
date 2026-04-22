#!/usr/bin/env python3
"""
Engram Retrieval Benchmark
===========================

Measures search quality of Engram's hybrid retrieval (FTS5 + semantic) using
a curated query set with known correct answers.

Metrics:
  - R@k (Recall at k): fraction of queries where the correct entry is in top-k
  - NDCG@k: normalized discounted cumulative gain
  - Per-category breakdown (exact_error, semantic_similar, tag_filter, type_inference)

Usage:
    # Run with seeded data (auto-seeds if DB empty):
    python benchmarks/engram_retrieval_bench.py

    # Run against specific DB:
    ENGRAM_DB_PATH=/tmp/test.db python benchmarks/engram_retrieval_bench.py

    # Compare search modes:
    python benchmarks/engram_retrieval_bench.py --mode fts_only
    python benchmarks/engram_retrieval_bench.py --mode semantic_only
    python benchmarks/engram_retrieval_bench.py --mode hybrid  # default

    # Limit queries for quick runs:
    python benchmarks/engram_retrieval_bench.py --limit 10

    # Verbose output (show top-5 results per query):
    python benchrams/engram_retrieval_bench.py --verbose
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


# ── Metrics ──────────────────────────────────────────────────────────────────

def _dcg(relevances: list[float], k: int) -> float:
    score = 0.0
    for i, rel in enumerate(relevances[:k]):
        score += rel / math.log2(i + 2)
    return score


def ndcg_at_k(retrieved_titles: list[str], expected_contains: str, k: int) -> float:
    """Compute NDCG@k where relevance=1 if expected_contains is in the title."""
    relevances = [
        1.0 if expected_contains.lower() in t.lower() else 0.0
        for t in retrieved_titles[:k]
    ]
    ideal = sorted(relevances, reverse=True)
    idcg = _dcg(ideal, k)
    if idcg == 0:
        return 0.0
    return _dcg(relevances, k) / idcg


def recall_at_k(retrieved_titles: list[str], expected_contains: str, k: int) -> float:
    """Return 1.0 if expected_contains found in any top-k title, else 0.0."""
    for title in retrieved_titles[:k]:
        if expected_contains.lower() in title.lower():
            return 1.0
    return 0.0


# ── Search Adapters ──────────────────────────────────────────────────────────

def _run_hybrid(query: str, limit: int, db_path: str | None) -> list[dict]:
    from src.search import search
    results = search(query, limit=limit, db_path=db_path)
    return list(results)


def _run_fts_only(query: str, limit: int, db_path: str | None) -> list[dict]:
    """FTS5-only search — bypasses semantic component."""
    from src.database import get_connection
    with get_connection(db_path) as conn:
        fts_query = " OR ".join(f'"{term}"' for term in query.strip().split() if term)
        rows = conn.execute(
            """SELECT item_type, item_id, title, content as snippet, tags, rank
               FROM memory_fts
               WHERE memory_fts MATCH ?
               ORDER BY rank
               LIMIT ?""",
            [fts_query, limit],
        ).fetchall()
        return [{"item_type": r["item_type"], "item_id": r["item_id"],
                 "title": r["title"], "snippet": r["snippet"] or "",
                 "tags": r["tags"], "is_semantic": False} for r in rows]


def _run_semantic_only(query: str, limit: int, db_path: str | None) -> list[dict]:
    """Semantic-only search — bypasses FTS5 component."""
    from src.search import semantic_search
    results, status = semantic_search(query, limit=limit, db_path=db_path)
    return results


SEARCH_MODES = {
    "hybrid": _run_hybrid,
    "fts_only": _run_fts_only,
    "semantic_only": _run_semantic_only,
}


# ── Benchmark Runner ─────────────────────────────────────────────────────────

def run_benchmark(
    queries: list[dict],
    mode: str = "hybrid",
    k_values: list[int] | None = None,
    db_path: str | None = None,
    verbose: bool = False,
) -> dict:
    """Run all queries and compute aggregate metrics.

    Returns a results dict with per-query details and aggregate R@k / NDCG@k.
    """
    k_values = k_values or [1, 3, 5, 10]
    search_fn = SEARCH_MODES[mode]

    per_query = []
    category_results: dict[str, list] = {}
    total_latency_ms = 0.0

    for q in queries:
        query_text = q["query"]
        expected_contains = q["expected_title_contains"]
        category = q.get("category", "unknown")

        t0 = time.perf_counter()
        try:
            results = search_fn(query_text, limit=max(k_values), db_path=db_path)
        except Exception as exc:
            results = []
            if verbose:
                print(f"  ERROR on {q['id']}: {exc}")
        elapsed_ms = (time.perf_counter() - t0) * 1000
        total_latency_ms += elapsed_ms

        retrieved_titles = [r.get("title", "") for r in results]

        recall_scores = {k: recall_at_k(retrieved_titles, expected_contains, k) for k in k_values}
        ndcg_scores = {k: ndcg_at_k(retrieved_titles, expected_contains, k) for k in k_values}

        row = {
            "id": q["id"],
            "query": query_text,
            "expected_contains": expected_contains,
            "category": category,
            "retrieved_titles": retrieved_titles[:max(k_values)],
            "recall": recall_scores,
            "ndcg": ndcg_scores,
            "latency_ms": elapsed_ms,
            "hit_at_1": recall_scores.get(1, 0.0) == 1.0,
        }
        per_query.append(row)
        category_results.setdefault(category, []).append(row)

        if verbose:
            status = "✓" if recall_scores.get(5, 0) == 1.0 else "✗"
            print(f"  {status} [{q['id']}] {query_text[:60]}")
            for i, t in enumerate(retrieved_titles[:5], 1):
                marker = "→" if expected_contains.lower() in t.lower() else " "
                print(f"      {marker} {i}. {t}")

    n = len(per_query)
    avg_latency_ms = total_latency_ms / n if n > 0 else 0.0

    aggregate = {
        "n_queries": n,
        "avg_latency_ms": round(avg_latency_ms, 1),
    }
    for k in k_values:
        aggregate[f"R@{k}"] = round(
            sum(r["recall"][k] for r in per_query) / n if n > 0 else 0.0, 4
        )
        aggregate[f"NDCG@{k}"] = round(
            sum(r["ndcg"][k] for r in per_query) / n if n > 0 else 0.0, 4
        )

    # Per-category aggregates
    by_category = {}
    for cat, rows in category_results.items():
        nc = len(rows)
        by_category[cat] = {
            "n": nc,
            **{
                f"R@{k}": round(
                    sum(r["recall"][k] for r in rows) / nc if nc > 0 else 0.0, 4
                )
                for k in k_values
            },
        }

    return {
        "mode": mode,
        "aggregate": aggregate,
        "by_category": by_category,
        "per_query": per_query,
    }


# ── Reporting ─────────────────────────────────────────────────────────────────

def _bar(value: float, width: int = 20) -> str:
    filled = round(value * width)
    return "█" * filled + "░" * (width - filled)


def print_report(results: dict) -> None:
    mode = results["mode"]
    agg = results["aggregate"]
    by_cat = results["by_category"]

    print(f"\n{'─' * 60}")
    print(f"  Engram Retrieval Benchmark — mode: {mode}")
    print(f"{'─' * 60}")
    print(f"  Queries: {agg['n_queries']}    Avg latency: {agg['avg_latency_ms']}ms")
    print()
    print("  Aggregate:")
    for metric in ["R@1", "R@3", "R@5", "R@10", "NDCG@5", "NDCG@10"]:
        if metric in agg:
            val = agg[metric]
            print(f"    {metric:<10} {val:.1%}  {_bar(val)}")

    print()
    print("  By category:")
    for cat, cat_agg in sorted(by_cat.items()):
        r5 = cat_agg.get("R@5", cat_agg.get("R@3", 0))
        print(f"    {cat:<20} R@5={r5:.1%}  n={cat_agg['n']}")

    print(f"\n{'─' * 60}\n")


def print_failures(results: dict, k: int = 5) -> None:
    """Print queries that failed at R@k."""
    failures = [r for r in results["per_query"] if r["recall"].get(k, 0) == 0.0]
    if not failures:
        print(f"  No failures at R@{k}.")
        return
    print(f"\n  Failures at R@{k} ({len(failures)} queries):")
    for r in failures:
        print(f"    [{r['id']}] {r['query']}")
        print(f"           expected: contains '{r['expected_contains']}'")
        titles = r["retrieved_titles"][:k]
        if titles:
            print(f"           got: {titles[0][:60]}...")
        else:
            print("           got: (no results)")


# ── Seed helper ───────────────────────────────────────────────────────────────

def _ensure_seeded(db_path: str | None) -> None:
    """Seed DB if empty so benchmark has data to work with."""
    from src.database import get_connection, init_db
    init_db(db_path)
    with get_connection(db_path) as conn:
        count = conn.execute("SELECT COUNT(*) as c FROM skills").fetchone()["c"]
    if count == 0:
        print("  Database empty — seeding with sample data...")
        from src.seed import seed_database
        seed_database(db_path)
        print("  Seed complete.\n")


# ── CLI ───────────────────────────────────────────────────────────────────────

def _load_queries(queries_file: str, limit: int | None = None) -> list[dict]:
    with open(queries_file) as f:
        data = json.load(f)
    queries = data["queries"]
    if limit:
        queries = queries[:limit]
    return queries


def compare_modes(queries: list[dict], k_values: list[int], db_path: str | None, verbose: bool) -> None:
    """Run all three modes and print a side-by-side comparison."""
    all_results = {}
    for mode in SEARCH_MODES:
        print(f"  Running mode: {mode}...")
        all_results[mode] = run_benchmark(queries, mode=mode, k_values=k_values,
                                          db_path=db_path, verbose=False)

    print(f"\n{'─' * 70}")
    print(f"  Mode Comparison ({len(queries)} queries)")
    print(f"{'─' * 70}")
    header = f"  {'Metric':<10}"
    for mode in SEARCH_MODES:
        header += f"  {mode:<18}"
    print(header)
    print(f"  {'─' * 65}")

    for metric in ["R@1", "R@3", "R@5", "R@10", "NDCG@5", "NDCG@10"]:
        row = f"  {metric:<10}"
        for mode in SEARCH_MODES:
            agg = all_results[mode]["aggregate"]
            val = agg.get(metric, 0.0)
            row += f"  {val:.1%} {_bar(val, 10):<12}"
        print(row)

    print(f"\n  {'Latency':<10}", end="")
    for mode in SEARCH_MODES:
        ms = all_results[mode]["aggregate"]["avg_latency_ms"]
        print(f"  {ms:>6.1f}ms{'':<11}", end="")
    print(f"\n{'─' * 70}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Engram retrieval benchmark")
    parser.add_argument(
        "--mode",
        choices=list(SEARCH_MODES) + ["compare"],
        default="hybrid",
        help="Search mode (default: hybrid; use 'compare' to run all three)",
    )
    parser.add_argument(
        "--queries",
        default=str(Path(__file__).parent / "test_queries.json"),
        help="Path to query dataset JSON file",
    )
    parser.add_argument("--limit", type=int, default=None,
                        help="Limit number of queries (for quick runs)")
    parser.add_argument("--k", nargs="+", type=int, default=[1, 3, 5, 10],
                        help="k values for R@k and NDCG@k (default: 1 3 5 10)")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Show top-5 results per query")
    parser.add_argument("--failures", action="store_true",
                        help="Print queries that failed R@5")
    parser.add_argument("--output", help="Write JSON results to this file")
    parser.add_argument("--no-seed", action="store_true",
                        help="Skip auto-seeding (use existing DB)")
    args = parser.parse_args()

    db_path = os.environ.get("ENGRAM_DB_PATH")

    if not args.no_seed:
        _ensure_seeded(db_path)

    queries = _load_queries(args.queries, args.limit)
    print(f"  Loaded {len(queries)} queries from {args.queries}")

    if args.mode == "compare":
        compare_modes(queries, args.k, db_path, args.verbose)
    else:
        if args.verbose:
            print(f"\n  Running {args.mode} search...\n")
        results = run_benchmark(
            queries, mode=args.mode, k_values=args.k,
            db_path=db_path, verbose=args.verbose,
        )
        print_report(results)
        if args.failures:
            print_failures(results)
        if args.output:
            with open(args.output, "w") as f:
                json.dump(results, f, indent=2)
            print(f"  Results written to {args.output}")


if __name__ == "__main__":
    main()
