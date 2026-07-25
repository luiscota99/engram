#!/usr/bin/env python3
"""Engram Engineering MemEval (EEME) — home-domain retrieval gate.

Runs the seeded retrieval suite (or a dedicated eeme query file) and reports
R@5 for the engineering niche Engram claims as SOTA.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fail-under-r5", type=float, default=0.90)
    ap.add_argument(
        "--queries",
        default=str(ROOT / "benchmarks" / "test_queries.json"),
        help="Labeled query JSON (default: curated 100-query engineering suite)",
    )
    args = ap.parse_args()
    env = os.environ.copy()
    if "ENGRAM_DB_PATH" not in env:
        env["ENGRAM_DB_PATH"] = str(Path(os.environ.get("TMPDIR", "/tmp")) / "engram_eeme.db")
    cmd = [
        sys.executable,
        str(ROOT / "benchmarks" / "engram_retrieval_bench.py"),
        "--queries",
        args.queries,
        "--fail-under-r5",
        str(args.fail_under_r5),
    ]
    print("EEME →", " ".join(cmd))
    print("ENGRAM_DB_PATH=", env["ENGRAM_DB_PATH"])
    return subprocess.call(cmd, cwd=str(ROOT), env=env)


if __name__ == "__main__":
    raise SystemExit(main())
