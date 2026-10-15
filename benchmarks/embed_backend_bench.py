#!/usr/bin/env python3
"""
Embedding-backend A/B benchmark
===============================

Compares embedding backends for the *hot path* (the recall/guard relevance
gate, which embeds on nearly every turn) on two axes:

  * embed latency  — the measured bottleneck (Ollama/nomic is CPU-bound at
                     ~1.3s/query on this hardware; the vector KNN is ~10ms)
  * retrieval quality — R@1 / R@5 / MRR on the seeded labeled query set

The gate tolerates ~15% quality loss for a large latency win, so this harness
exists to check whether a fast in-process backend (Model2Vec static / FastEmbed
ONNX) is good enough to replace nomic on the hot path.

Design
------
Each backend runs in its **own subprocess** with a **fresh scratch DB** built at
that backend's native vector dimension (nomic=768, potion=512, bge-small=384) —
a static query vector cannot be compared to a nomic document vector, so the docs
must be embedded by the same backend. This is the single-space validation; the
production two-tier design would keep both spaces in parallel tables.

Usage
-----
    python benchmarks/embed_backend_bench.py
    python benchmarks/embed_backend_bench.py --backends static fastembed
    python benchmarks/embed_backend_bench.py --latency-n 20 --limit 50

No ENGRAM_DB_PATH needed — the harness manages per-backend scratch DBs under a
temp dir (and refuses to touch the real DB).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

# name -> extra env applied to the child process
BACKENDS: dict[str, dict[str, str]] = {
    "ollama": {},  # default backend; requires a running Ollama
    "static": {"ENGRAM_EMBED_BACKEND": "static"},
    "fastembed": {"ENGRAM_EMBED_BACKEND": "fastembed"},
}

RESULT_SENTINEL = "@@BACKEND_RESULT@@"


# ── child: measure one backend ────────────────────────────────────────────────

def _percentile(samples: list[float], q: float) -> float | None:
    if not samples:
        return None
    ordered = sorted(samples)
    idx = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return round(ordered[idx], 2)


def _prepare_scratch_db(db_path: str) -> tuple[bool, str, int]:
    """Build a fresh scratch DB at the active backend's native dimension.

    Returns (ok, reason, dim). Embeds one probe to discover the output dim, then
    rebuilds vec_memory to match so the seeded document vectors are storable.
    """
    from src import embeddings
    from src.database import (
        get_connection,
        init_db,
        rebuild_vec_table,
        set_schema_meta,
    )

    for suffix in ("", "-shm", "-wal"):
        try:
            os.remove(db_path + suffix)
        except OSError:
            pass
    cache_db = os.path.join(os.path.dirname(db_path), "embed_cache.db")
    try:
        os.remove(cache_db)
    except OSError:
        pass

    init_db(db_path)

    embeddings.clear_embedding_cache()
    probe = embeddings.embed_text("dimension probe")
    if not probe:
        return False, embeddings.get_embedding_degradation_reason() or "no_embedding", 0
    dim = len(probe)
    if dim != 768:
        with get_connection(db_path) as conn:
            rebuild_vec_table(dim, conn)
    set_schema_meta("vec_dimension", str(dim), db_path=db_path)
    return True, "", dim


def _latency_samples(queries: list[str], n: int) -> list[float]:
    """Time raw embed cost with caches disabled; model warmed once beforehand."""
    from src import embeddings

    os.environ["ENGRAM_EMBED_CACHE"] = "off"  # never serve from persistent cache
    embeddings.warm_up()  # force model load / connection so it's not counted
    samples: list[float] = []
    for q in queries[:n]:
        embeddings.clear_embedding_cache()  # defeat the in-process LRU too
        t0 = time.perf_counter()
        vec = embeddings.embed_text(q)
        dt = (time.perf_counter() - t0) * 1000.0
        if vec:
            samples.append(dt)
    return samples


def run_one(backend: str, queries_file: str, limit: int | None, latency_n: int) -> dict:
    from benchmarks.engram_retrieval_bench import _load_queries, run_benchmark
    from src import embeddings
    from src.seed import seed_database

    result: dict = {"backend": backend, "available": False}

    scratch_dir = tempfile.mkdtemp(prefix=f"engram_embedbench_{backend}_")
    db_path = os.path.join(scratch_dir, "bench.db")
    os.environ["ENGRAM_DB_PATH"] = db_path

    ok, reason, dim = _prepare_scratch_db(db_path)
    if not ok:
        result["reason"] = reason
        return result

    result["available"] = True
    result["dim"] = dim
    result["model"] = embeddings.get_embedding_model()

    t_seed = time.perf_counter()
    seed_database(db_path)
    result["seed_secs"] = round(time.perf_counter() - t_seed, 1)

    queries = _load_queries(queries_file, limit)
    query_texts = [q["query"] for q in queries]

    # Latency FIRST, on the freshly-warmed model: the retrieval benches below
    # fire hundreds of embeds and, for Ollama, a couple of load-induced timeouts
    # trip the dead-host cooldown (see embeddings.MISTAKE #16) — which would
    # poison the very latency numbers we came to measure.
    samples = _latency_samples(query_texts, latency_n)
    result["embed_ms"] = {
        "n": len(samples),
        "p50": _percentile(samples, 0.50),
        "p95": _percentile(samples, 0.95),
        "mean": round(sum(samples) / len(samples), 2) if samples else None,
    }

    # Quality: hybrid (production path) + semantic-only (isolates the embedding).
    hybrid = run_benchmark(queries, mode="hybrid", db_path=db_path)["aggregate"]
    semantic = run_benchmark(queries, mode="semantic_only", db_path=db_path)["aggregate"]
    result["hybrid"] = {k: hybrid[k] for k in ("R@1", "R@5", "MRR", "avg_latency_ms")}
    result["semantic"] = {k: semantic[k] for k in ("R@1", "R@5", "MRR")}
    return result


# ── parent: orchestrate + report ──────────────────────────────────────────────

def _spawn(backend: str, args: argparse.Namespace) -> dict:
    env = dict(os.environ)
    env.update(BACKENDS[backend])
    env.pop("ENGRAM_DB_PATH", None)  # child creates its own scratch DB
    cmd = [
        sys.executable,
        str(Path(__file__)),
        "--run-one",
        backend,
        "--queries",
        args.queries,
        "--latency-n",
        str(args.latency_n),
    ]
    if args.limit:
        cmd += ["--limit", str(args.limit)]
    print(f"  running backend: {backend} ...", flush=True)
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True)
    for line in proc.stdout.splitlines():
        if line.startswith(RESULT_SENTINEL):
            return json.loads(line[len(RESULT_SENTINEL):])
    return {
        "backend": backend,
        "available": False,
        "reason": f"child_error (exit {proc.returncode}): {proc.stderr.strip()[-300:]}",
    }


def _fmt(v, suffix: str = "", width: int = 12) -> str:
    return (f"{v}{suffix}" if v is not None else "—").ljust(width)


def print_comparison(results: list[dict]) -> None:
    print(f"\n{'─' * 78}")
    print("  Embedding-backend A/B — hot-path latency vs retrieval quality")
    print(f"{'─' * 78}")
    header = f"  {'metric':<16}" + "".join(f"{r['backend']:<14}" for r in results)
    print(header)
    print(f"  {'─' * 74}")

    def row(label: str, getter) -> None:
        line = f"  {label:<16}"
        for r in results:
            line += _fmt(getter(r) if r.get("available") else None, width=14)
        print(line)

    row("dim", lambda r: r.get("dim"))
    row("embed p50 (ms)", lambda r: r["embed_ms"]["p50"])
    row("embed p95 (ms)", lambda r: r["embed_ms"]["p95"])
    row("embed mean (ms)", lambda r: r["embed_ms"]["mean"])
    row("seed (s)", lambda r: r.get("seed_secs"))
    row("hybrid R@5", lambda r: r["hybrid"]["R@5"])
    row("hybrid MRR", lambda r: r["hybrid"]["MRR"])
    row("semantic R@5", lambda r: r["semantic"]["R@5"])
    row("semantic MRR", lambda r: r["semantic"]["MRR"])

    print(f"  {'─' * 74}")
    for r in results:
        if not r.get("available"):
            print(f"  {r['backend']}: SKIPPED — {r.get('reason', 'unavailable')}")

    # Deltas vs the ollama baseline, if present.
    base = next((r for r in results if r["backend"] == "ollama" and r.get("available")), None)
    if base:
        b_p50 = base["embed_ms"]["p50"]
        b_r5 = base["semantic"]["R@5"]
        print(f"\n  vs ollama baseline (embed p50 {b_p50}ms, semantic R@5 {b_r5}):")
        for r in results:
            if r["backend"] == "ollama" or not r.get("available"):
                continue
            p50 = r["embed_ms"]["p50"]
            if b_p50 and p50:
                speedup = f"{round(b_p50 / p50, 1)}x faster embed"
            else:
                speedup = "embed speedup n/a"
            dr5 = round(r["semantic"]["R@5"] - b_r5, 3)
            rel = round(100 * r["semantic"]["R@5"] / b_r5, 1) if b_r5 else None
            print(
                f"    {r['backend']:<10} {speedup}   "
                f"semantic R@5 {r['semantic']['R@5']} ({rel}% of baseline, Δ{dr5:+})"
            )
    print(f"{'─' * 78}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Embedding-backend A/B benchmark")
    parser.add_argument("--run-one", metavar="BACKEND", help="internal: measure a single backend")
    parser.add_argument("--backends", nargs="+", default=list(BACKENDS),
                        choices=list(BACKENDS), help="backends to compare")
    parser.add_argument("--queries", default=str(Path(__file__).parent / "test_queries.json"))
    parser.add_argument("--limit", type=int, default=None, help="limit query count")
    parser.add_argument("--latency-n", type=int, default=25,
                        help="how many embeds to time for the latency percentiles")
    parser.add_argument("--output", help="write the raw results JSON here")
    args = parser.parse_args()

    if args.run_one:
        res = run_one(args.run_one, args.queries, args.limit, args.latency_n)
        print(RESULT_SENTINEL + json.dumps(res))
        return

    results = [_spawn(b, args) for b in args.backends]
    print_comparison(results)
    if args.output:
        with open(args.output, "w") as f:
            json.dump(results, f, indent=2)
        print(f"  raw results → {args.output}")


if __name__ == "__main__":
    main()
