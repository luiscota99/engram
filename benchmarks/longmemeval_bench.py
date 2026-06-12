#!/usr/bin/env python3
"""
LongMemEval adapter for Engram hybrid search.

Maps LongMemEval-style question → memory retrieval via ``search()`` and reports
R@5 and MRR. Falls back to bundled offline samples when HuggingFace download
is unavailable (``--offline``).

Usage:
    python benchmarks/longmemeval_bench.py
    python benchmarks/longmemeval_bench.py --offline
    python benchmarks/longmemeval_bench.py --fail-under-r5 0.50
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
    mrr_from_relevances,
    recall_at_k_from_relevances,
    relevances_from_results,
)

BENCH_DIR = Path(__file__).parent
DEFAULT_OUTPUT = BENCH_DIR / "longmemeval_results.json"
OFFLINE_SAMPLES = BENCH_DIR / "longmemeval_offline_samples.json"


def _ensure_seeded(db_path: str | None) -> None:
    from src.database import get_connection, init_db

    init_db(db_path)
    with get_connection(db_path) as conn:
        count = conn.execute("SELECT COUNT(*) as c FROM skills").fetchone()["c"]
    if count == 0:
        print("  Database empty — seeding with sample data...")
        from src.seed import seed_database

        seed_database(db_path)
        print("  Seed complete.\n")


def _load_offline_samples() -> list[dict]:
    if OFFLINE_SAMPLES.is_file():
        with open(OFFLINE_SAMPLES) as f:
            data = json.load(f)
        return data.get("queries", data)
    return _builtin_offline_samples()


def _builtin_offline_samples() -> list[dict]:
    """Minimal LongMemEval-style rows when no HF or bundled file."""
    return [
        {
            "id": "lme-off-001",
            "query": "N+1 database query loop performance fix",
            "expected_type": "mistake",
            "expected_item_id": 1,
            "category": "offline_sample",
        },
        {
            "id": "lme-off-002",
            "query": "React useEffect infinite render loop dependency array",
            "expected_type": "mistake",
            "expected_item_id": 3,
            "category": "offline_sample",
        },
        {
            "id": "lme-off-003",
            "query": "race condition async fetch AbortController",
            "expected_type": "pattern",
            "expected_item_id": 1,
            "category": "offline_sample",
        },
        {
            "id": "lme-off-004",
            "query": "safe production database migration workflow",
            "expected_type": "skill",
            "expected_item_id": 1,
            "category": "offline_sample",
        },
        {
            "id": "lme-off-005",
            "query": "debugging stack trace without guessing fix",
            "expected_type": "skill",
            "expected_item_id": 2,
            "category": "offline_sample",
        },
    ]


def _load_longmemeval_hf(limit: int | None = None) -> list[dict]:
    """Download LongMemEval from HuggingFace and map to Engram query format."""
    from datasets import load_dataset

    ds = load_dataset("xiaowu0162/longmemeval-cleaned", split="train")
    queries: list[dict] = []
    for i, row in enumerate(ds):
        if limit and i >= limit:
            break
        question = row.get("question") or row.get("query") or ""
        if not question.strip():
            continue
        entry: dict = {
            "id": f"lme-{i:04d}",
            "query": question.strip(),
            "category": "longmemeval",
            "notes": "Mapped from LongMemEval HF dataset; ground truth may be absent on seed DB",
        }
        if row.get("answer"):
            entry["expected_title_contains"] = str(row["answer"])[:80]
        queries.append(entry)
    return queries


def _load_queries(offline: bool, limit: int | None, queries_file: str | None) -> tuple[list[dict], str]:
    if queries_file:
        with open(queries_file) as f:
            data = json.load(f)
        queries = data.get("queries", data)
        source = queries_file
    elif offline:
        queries = _load_offline_samples()
        source = str(OFFLINE_SAMPLES) if OFFLINE_SAMPLES.is_file() else "builtin"
    else:
        try:
            queries = _load_longmemeval_hf(limit=limit)
            source = "huggingface:xiaowu0162/longmemeval-cleaned"
        except Exception as exc:
            print(f"  HF download failed ({exc}); falling back to offline samples.", file=sys.stderr)
            queries = _load_offline_samples()
            source = "offline-fallback"
    if limit:
        queries = queries[:limit]
    return queries, source


def run_longmemeval(
    queries: list[dict],
    db_path: str | None = None,
    k: int = 5,
) -> dict:
    from src.search import search

    per_query: list[dict] = []
    category_buckets: dict[str, list[dict]] = {}
    total_ms = 0.0

    for q in queries:
        t0 = time.perf_counter()
        try:
            results = search(q["query"], limit=k, db_path=db_path, skip_audit=True)
            hits = list(results)
        except Exception as exc:
            hits = []
            q = {**q, "error": str(exc)}
        elapsed_ms = (time.perf_counter() - t0) * 1000
        total_ms += elapsed_ms

        rel = relevances_from_results(hits, q)
        r_at_k = recall_at_k_from_relevances(rel, k)
        mrr = mrr_from_relevances(rel)

        row = {
            "id": q.get("id"),
            "query": q["query"],
            "category": q.get("category", "unknown"),
            "R@5": r_at_k if k == 5 else recall_at_k_from_relevances(rel, 5),
            "mrr": mrr,
            "latency_ms": round(elapsed_ms, 2),
            "hit_at_5": r_at_k == 1.0,
        }
        per_query.append(row)
        category_buckets.setdefault(row["category"], []).append(row)

    n = len(per_query)
    aggregate = {
        "n_queries": n,
        "R@5": round(sum(r["R@5"] for r in per_query) / n if n else 0.0, 4),
        "MRR": round(sum(r["mrr"] for r in per_query) / n if n else 0.0, 4),
        "avg_latency_ms": round(total_ms / n if n else 0.0, 1),
    }

    by_category = {}
    for cat, rows in category_buckets.items():
        nc = len(rows)
        by_category[cat] = {
            "n": nc,
            "R@5": round(sum(r["R@5"] for r in rows) / nc if nc else 0.0, 4),
            "MRR": round(sum(r["mrr"] for r in rows) / nc if nc else 0.0, 4),
        }

    return {
        "benchmark": "longmemeval",
        "aggregate": aggregate,
        "by_category": by_category,
        "per_query": per_query,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="LongMemEval adapter for Engram search")
    parser.add_argument("--offline", action="store_true", help="Use bundled offline samples only")
    parser.add_argument("--queries", help="Custom query JSON (same schema as test_queries.json)")
    parser.add_argument("--limit", type=int, default=None, help="Max queries to run")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="JSON output path")
    parser.add_argument("--no-seed", action="store_true", help="Skip auto-seed on empty DB")
    parser.add_argument(
        "--fail-under-r5",
        type=float,
        default=None,
        metavar="SCORE",
        help="Exit 1 if aggregate R@5 is below SCORE",
    )
    args = parser.parse_args()

    db_path = os.environ.get("ENGRAM_DB_PATH")
    if not args.no_seed:
        _ensure_seeded(db_path)

    queries, source = _load_queries(args.offline, args.limit, args.queries)
    print(f"  Loaded {len(queries)} queries from {source}")

    results = run_longmemeval(queries, db_path=db_path)
    results["source"] = source

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2)

    agg = results["aggregate"]
    print(f"\n  LongMemEval adapter — R@5={agg['R@5']:.4f}  MRR={agg['MRR']:.4f}  n={agg['n_queries']}")
    print(f"  Results written to {out_path}")

    if args.fail_under_r5 is not None:
        if agg["R@5"] < args.fail_under_r5:
            print(
                f"  FAIL: R@5={agg['R@5']:.4f} below threshold {args.fail_under_r5:.4f}",
                file=sys.stderr,
            )
            sys.exit(1)
        print(f"  PASS: R@5={agg['R@5']:.4f} >= {args.fail_under_r5:.4f}")


if __name__ == "__main__":
    main()
