#!/usr/bin/env python3
"""BEAM-style adapter stub — honest placeholder for Beyond-a-Million-Tokens evals.

Engram does not vendor the BEAM corpus. This entrypoint documents how to plug a
local labeled query file (same schema as ``benchmarks/test_queries.json``) and
runs retrieval-only grading. It never claims Hindsight's published BEAM QA
numbers (different metric family — see ``docs/COMPARISON.md``).
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
    ap.add_argument(
        "--queries",
        help="Path to labeled queries JSON. If omitted, prints setup instructions.",
    )
    ap.add_argument("--fail-under-r5", type=float, default=0.0)
    args = ap.parse_args()
    if not args.queries:
        print(
            "BEAM adapter (retrieval-only).\n"
            "Provide a local labeled query file:\n"
            "  python benchmarks/beam_bench.py --queries /path/to/beam_slice.json\n"
            "Metric footnote: Engram reports R@k retrieval, not BEAM QA accuracy."
        )
        return 0
    path = Path(args.queries)
    if not path.is_file():
        print(f"Missing queries file: {path}", file=sys.stderr)
        return 2
    env = os.environ.copy()
    env.setdefault("ENGRAM_DB_PATH", "/tmp/engram_beam.db")
    cmd = [
        sys.executable,
        str(ROOT / "benchmarks" / "engram_retrieval_bench.py"),
        "--queries",
        str(path),
        "--fail-under-r5",
        str(args.fail_under_r5),
    ]
    print("BEAM adapter →", " ".join(cmd))
    return subprocess.call(cmd, cwd=str(ROOT), env=env)


if __name__ == "__main__":
    raise SystemExit(main())
