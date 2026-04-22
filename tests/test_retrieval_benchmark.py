"""Regression: curated retrieval benchmark should stay above a minimum R@5."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from src.database import init_db
from src.seed import seed_database


@pytest.fixture
def bench_db(tmp_path: Path):
    db = str(tmp_path / "bench.db")
    os.environ["ENGRAM_DB_PATH"] = db
    init_db(db)
    seed_database(db)
    return db


def test_hybrid_recall_at_5_min(bench_db: str) -> None:
    from benchmarks.engram_retrieval_bench import _load_queries, run_benchmark

    root = Path(__file__).resolve().parent.parent
    qfile = root / "benchmarks" / "test_queries.json"
    queries = _load_queries(str(qfile), limit=None)
    out = run_benchmark(queries, mode="hybrid", k_values=[1, 3, 5, 10], db_path=bench_db)
    r5 = out["aggregate"]["R@5"]
    # Guardrail: if this fails, check ranking/search before merging
    assert r5 >= 0.85, f"R@5={r5} below 0.85 — see benchmarks/BENCHMARKS.md"


def test_fts5_tag_phrase_for_hyphenated_tags() -> None:
    from src.search import _fts5_tag_phrase

    assert _fts5_tag_phrase("ai-assistant") == '"ai-assistant"'
    assert _fts5_tag_phrase('say "hi"') == '"say ""hi"""'
