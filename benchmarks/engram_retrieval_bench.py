#!/usr/bin/env python3
"""
Engram Retrieval Benchmark
===========================

Measures search quality of Engram's hybrid retrieval (FTS5 + semantic) using
a curated query set with known correct answers.

Metrics:
  - R@k (Recall at k): fraction of queries where the correct entry is in top-k
  - MRR: mean reciprocal rank of the first correct hit (in full ranked list)
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
    python benchmarks/engram_retrieval_bench.py --verbose
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from benchmarks.grading import (  # noqa: E402
    abstention_success,
    expects_abstention,
    mrr_from_relevances,
    ndcg_at_k_from_relevances,
    recall_at_k,
    recall_at_k_from_relevances,
    relevances_from_results,
    row_matches_expected,
    use_id_grading,
)

# ── Metrics (relevance vectors; see grading.py) ─────────────────────────────


# ── Search Adapters ──────────────────────────────────────────────────────────

def _run_hybrid(query: str, limit: int, db_path: str | None) -> list[dict]:
    from src.search import search
    results = search(query, limit=limit, db_path=db_path, skip_audit=True)
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

def _trim_hit(r: dict) -> dict:
    return {
        "item_type": r.get("item_type"),
        "item_id": r.get("item_id"),
        "title": (r.get("title") or "")[:200],
        "utility_score": round(float(r.get("utility_score", 0.0)), 6),
    }


def run_benchmark(
    queries: list[dict],
    mode: str = "hybrid",
    k_values: list[int] | None = None,
    db_path: str | None = None,
    verbose: bool = False,
    include_hit_detail: bool = False,
) -> dict:
    """Run all queries and compute aggregate metrics.

    Returns a results dict with per-query details and aggregate R@k / NDCG@k.
    When *include_hit_detail* is True, each per-query row may include
    *top_hits_detail* (for JSON tuning dumps).
    """
    k_values = k_values or [1, 3, 5, 10]
    search_fn = SEARCH_MODES[mode]
    kmax = max(k_values)

    per_query: list[dict] = []
    category_results: dict[str, list] = {}
    total_latency_ms = 0.0

    for q in queries:
        query_text = q["query"]
        expected_contains = q.get("expected_title_contains") or ""
        category = q.get("category", "unknown")
        grade_mode = "id" if use_id_grading(q) else "title"

        t0 = time.perf_counter()
        try:
            results = search_fn(query_text, limit=kmax, db_path=db_path)
        except Exception as exc:
            results = []
            if verbose:
                print(f"  ERROR on {q['id']}: {exc}")
        elapsed_ms = (time.perf_counter() - t0) * 1000
        total_latency_ms += elapsed_ms

        rel = relevances_from_results(results, q)
        if expects_abstention(q):
            recall_scores = {k: recall_at_k(results, q, k) for k in k_values}
            mrr = 1.0 if abstention_success(results, q, kmax) else 0.0
            ndcg_scores = {k: recall_scores[k] for k in k_values}
        else:
            recall_scores = {k: recall_at_k_from_relevances(rel, k) for k in k_values}
            ndcg_scores = {k: ndcg_at_k_from_relevances(rel, k) for k in k_values}
            mrr = mrr_from_relevances(rel)

        retrieved_titles = [r.get("title", "") for r in results]

        row: dict = {
            "id": q["id"],
            "query": query_text,
            "expected_contains": expected_contains,
            "grading": grade_mode,
            "expected_type": q.get("expected_type"),
            "expected_item_id": q.get("expected_item_id"),
            "category": category,
            "retrieved_titles": retrieved_titles[:kmax],
            "recall": recall_scores,
            "ndcg": ndcg_scores,
            "mrr": mrr,
            "latency_ms": elapsed_ms,
            "hit_at_1": recall_scores.get(1, 0.0) == 1.0,
        }
        if include_hit_detail:
            row["top_hits_detail"] = [_trim_hit(r) for r in results[:kmax]]
        per_query.append(row)
        category_results.setdefault(category, []).append(row)

        if verbose:
            status = "✓" if recall_scores.get(5, 0) == 1.0 else "✗"
            print(f"  {status} [{q['id']}] {query_text[:60]}")
            for i, r in enumerate(results[:5], 1):
                t = r.get("title", "")
                marker = "→" if row_matches_expected(r, q) else " "
                print(f"      {marker} {i}. {t}")

    n = len(per_query)
    avg_latency_ms = total_latency_ms / n if n > 0 else 0.0

    mrr_sum = sum(r.get("mrr", 0.0) for r in per_query)

    aggregate = {
        "n_queries": n,
        "avg_latency_ms": round(avg_latency_ms, 1),
        "MRR": round(mrr_sum / n if n > 0 else 0.0, 4),
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

    failed_query_ids = [r["id"] for r in per_query if r["recall"].get(5, 0) == 0.0]

    return {
        "mode": mode,
        "aggregate": aggregate,
        "by_category": by_category,
        "per_query": per_query,
        "failed_query_ids": failed_query_ids,
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
    print(f"  Queries: {agg['n_queries']}    Avg latency: {agg['avg_latency_ms']}ms    MRR: {agg.get('MRR', 0):.3f}")
    print()
    print("  Aggregate:")
    for metric in ["R@1", "R@3", "R@5", "R@10", "MRR", "NDCG@5", "NDCG@10"]:
        if metric in agg:
            val = agg[metric]
            if metric == "MRR":
                print(f"    {metric:<10} {val:.3f}")
            else:
                print(f"    {metric:<10} {val:.1%}  {_bar(val)}")

    print()
    print("  By category:")
    for cat, cat_agg in sorted(by_cat.items()):
        r5 = cat_agg.get("R@5", cat_agg.get("R@3", 0))
        print(f"    {cat:<20} R@5={r5:.1%}  n={cat_agg['n']}")

    print(f"\n{'─' * 60}\n")


def print_failures(results: dict, k: int = 5, detail: bool = False) -> None:
    """Print queries that failed at R@k."""
    failures = [r for r in results["per_query"] if r["recall"].get(k, 0) == 0.0]
    if not failures:
        print(f"  No failures at R@{k}.")
        return
    print(f"\n  Failures at R@{k} ({len(failures)} queries):")
    for r in failures:
        print(f"    [{r['id']}] {r['query']}")
        if r.get("grading") == "id" and r.get("expected_item_id") is not None:
            print(
                f"           expected: type={r.get('expected_type')} id={r['expected_item_id']}",
            )
        else:
            print(f"           expected: title contains '{r['expected_contains']}'")
        titles = r["retrieved_titles"][:k]
        if titles:
            print(f"           got: {titles[0][:60]}...")
        else:
            print("           got: (no results)")
        if detail and r.get("top_hits_detail"):
            print("           top hits (item_type, item_id, utility_score):")
            for h in r["top_hits_detail"][:k]:
                print(
                    f"             {h['item_type']}:{h['item_id']}  "
                    f"score={h['utility_score']}  {h['title'][:50]}...",
                )


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

    for metric in ["R@1", "R@3", "R@5", "R@10", "MRR", "NDCG@5", "NDCG@10"]:
        row = f"  {metric:<10}"
        for mode in SEARCH_MODES:
            agg = all_results[mode]["aggregate"]
            val = agg.get(metric, 0.0)
            if metric == "MRR":
                row += f"  {val:.3f}            "
            else:
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
    parser.add_argument(
        "--failure-detail",
        action="store_true",
        help="With --failures/--output: include top-k item_type, item_id, utility_score per query",
    )
    parser.add_argument("--output", help="Write JSON results to this file")
    parser.add_argument("--no-seed", action="store_true",
                        help="Skip auto-seeding (use existing DB)")
    parser.add_argument(
        "--fail-under-r5",
        type=float,
        default=None,
        metavar="SCORE",
        help="Exit with code 1 if aggregate R@5 is below SCORE (e.g. 0.90)",
    )
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
            queries,
            mode=args.mode,
            k_values=args.k,
            db_path=db_path,
            verbose=args.verbose,
            include_hit_detail=args.failure_detail,
        )
        print_report(results)
        if args.failures:
            print_failures(results, detail=args.failure_detail)
        if args.output:
            with open(args.output, "w") as f:
                json.dump(results, f, indent=2)
            print(f"  Results written to {args.output}")
        if args.fail_under_r5 is not None:
            r5 = results["aggregate"].get("R@5", 0.0)
            if r5 < args.fail_under_r5:
                print(
                    f"  FAIL: R@5={r5:.4f} below threshold {args.fail_under_r5:.4f}",
                    file=sys.stderr,
                )
                sys.exit(1)
            print(f"  PASS: R@5={r5:.4f} >= {args.fail_under_r5:.4f}")


if __name__ == "__main__":
    main()
