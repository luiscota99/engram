#!/usr/bin/env python3
"""
Inject synthetic distractor memories into a DB, then run the retrieval benchmark.

Use this to stress-test hybrid ranking with a **reproducible** haystack (unlike ad-hoc
runs on an unlabeled ~/.engram/memory.db).

Example:

  # Fresh temp DB: seed + 40 distractors + benchmark
  python benchmarks/inject_noise.py --distractors 40

  # Existing DB copy (path must be writable)
  python benchmarks/inject_noise.py --distractors 25 --db /tmp/noise_test.db
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _insert_distractors(db_path: str, n: int) -> None:
    from src.database import get_connection, index_in_fts, init_db, link_tags

    init_db(db_path)
    templates = [
        (
            "mistake",
            "Distractor: performance error loop schema",
            ["performance", "error", "loop", "schema", "backend"],
        ),
        (
            "skill",
            "Distractor workflow: API validation migration",
            ["api", "workflow", "migration", "validation", "frontend"],
        ),
    ]
    with get_connection(db_path) as conn:
        for i in range(n):
            kind, body, tags = templates[i % len(templates)]
            if kind == "mistake":
                cur = conn.execute(
                    """INSERT INTO mistakes
                       (date, context, mistake, root_cause, fix, prevention, conversation_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        "2026-01-01",
                        f"noise context {i}",
                        f"{body} #{i} " + "x" * min(40, i + 5),
                        "noise",
                        "noise",
                        "noise",
                        "conv-noise",
                    ),
                )
                mid = cur.lastrowid
                link_tags(conn, "mistake", mid, tags)
                title = body[:80]
                content = f"{body} #{i}"
                index_in_fts(conn, "mistake", mid, title, content, tags)
            else:
                cur = conn.execute(
                    """INSERT INTO skills
                       (name, domain, trigger_desc, workflow, pitfalls, key_files, dependencies)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        f"Distractor Skill {i}",
                        "engineering",
                        f"trigger noise {i}",
                        "step one\nstep two",
                        "pitfall",
                        "[]",
                        "none",
                    ),
                )
                sid = cur.lastrowid
                link_tags(conn, "skill", sid, tags)
                index_in_fts(
                    conn,
                    "skill",
                    sid,
                    f"Distractor Skill {i}",
                    f"{body} #{i}",
                    tags,
                )


def main() -> None:
    parser = argparse.ArgumentParser(description="Inject distractor rows and run retrieval benchmark")
    parser.add_argument("--distractors", type=int, default=30, help="Number of synthetic rows (default: 30)")
    parser.add_argument(
        "--db",
        help="Writable DB path; default: temp copy (seeded + noise)",
    )
    parser.add_argument(
        "--skip-bench",
        action="store_true",
        help="Only inject; do not run engram_retrieval_bench.py",
    )
    args = parser.parse_args()

    sys.path.insert(0, str(ROOT))

    if args.db:
        db_path = os.path.abspath(args.db)
        if not os.path.exists(db_path):
            from src.database import init_db
            from src.seed import seed_database

            init_db(db_path)
            seed_database(db_path)
    else:
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        db_path = tmp.name
        os.environ["ENGRAM_DB_PATH"] = db_path
        from src.database import init_db
        from src.seed import seed_database

        init_db(db_path)
        seed_database(db_path)

    os.environ["ENGRAM_DB_PATH"] = db_path
    _insert_distractors(db_path, args.distractors)
    print(f"  Injected {args.distractors} distractors into {db_path}\n")

    if args.skip_bench:
        print("  --skip-bench: run manually:")
        print(f"    ENGRAM_DB_PATH={db_path} python benchmarks/engram_retrieval_bench.py --no-seed")
        return

    bench = ROOT / "benchmarks" / "engram_retrieval_bench.py"
    subprocess.check_call(
        [sys.executable, str(bench), "--no-seed"],
        cwd=str(ROOT),
        env={**os.environ, "ENGRAM_DB_PATH": db_path},
    )


if __name__ == "__main__":
    main()
