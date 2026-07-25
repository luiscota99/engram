#!/usr/bin/env python3
"""Minimal LoCoMo-style adapter stub — retrieval grading when a local file is provided.

Does not download datasets. Pass ``--queries`` JSON with
``{id, query, expected_title_contains}`` entries (same shape as Engram benches).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--queries", required=True, help="Path to labeled queries JSON")
    ap.add_argument("--fail-under-r5", type=float, default=0.0)
    args = ap.parse_args()
    path = Path(args.queries)
    if not path.is_file():
        print(f"Missing queries file: {path}", file=sys.stderr)
        return 2
    data = json.loads(path.read_text())
    n = len(data.get("queries") or data if isinstance(data, list) else [])
    print(f"LoCoMo adapter: {n} queries from {path}")
    print("Metric: retrieval R@k (NOT LLM-as-judge QA). See docs/COMPARISON.md.")
    env = os.environ.copy()
    env.setdefault("ENGRAM_DB_PATH", "/tmp/engram_locomo.db")
    cmd = [
        sys.executable,
        str(ROOT / "benchmarks" / "engram_retrieval_bench.py"),
        "--queries",
        str(path),
        "--fail-under-r5",
        str(args.fail_under_r5),
    ]
    return subprocess.call(cmd, cwd=str(ROOT), env=env)


if __name__ == "__main__":
    raise SystemExit(main())
