#!/usr/bin/env python3
"""Measure the Action Ladder's per-rung token cost — reproducibly.

Answers "are the token numbers real?": the reflex/recall/reason figures are the
MEASURED `engram route` output size (Engram's injected cost), using the same
~4 chars/token heuristic as the other benchmarks. The 'reason' rung's headline
"1000s of tokens" is the COUNTERFACTUAL cost of unaided LLM reasoning that
recall/reflex let you avoid — it is not an Engram measurement and is labelled
as such.

    ENGRAM_DB_PATH=/tmp/ladder.db ENGRAM_EMBED_URL=disabled \
        python benchmarks/ladder_cost.py
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.database import get_connection  # noqa: E402
from src.memory_ops import create_skill  # noqa: E402
from src.reflex import approve_reflex, promote_skill  # noqa: E402
from src.router import route_task  # noqa: E402


def _tok(text: str) -> int:
    return max(1, len(text) // 4)


def measure() -> dict:
    with get_connection() as c:
        sid = create_skill(
            c, name="Deploy Rollback", domain="ops",
            trigger="rollback a failed deploy",
            workflow="1. stop workers 2. revert image 3. redeploy 4. verify health",
        )
    with patch("src.llm.is_llm_available", return_value=False):
        r = promote_skill(sid)
    with get_connection() as c:
        c.execute("UPDATE reflexes SET script='echo ok' WHERE id=?", (r["id"],))
    approve_reflex(r["id"], read_only=True)

    reflex_out = _tok(route_task("rollback the failed deploy")["text"])
    with get_connection() as c:
        c.execute("DELETE FROM reflexes")
    recall_out = _tok(route_task("rollback the failed deploy")["text"])
    reason_out = _tok(route_task("configure the quantum flux capacitor timings")["text"])
    return {"reflex": reflex_out, "recall": recall_out, "reason": reason_out}


def main() -> None:
    m = measure()
    print("Action Ladder — measured `engram route` output tokens per rung:")
    print(f"  REFLEX  {m['reflex']:>4} tok   (+ ~12 tok for the deterministic tool call)")
    print(f"  RECALL  {m['recall']:>4} tok   (route guidance; reading full skill via read_item adds more)")
    print(f"  REASON  {m['reason']:>4} tok   route output only — the *task* then costs 1000s of")
    print("                    reasoning tokens Engram cannot measure and does not claim.")


if __name__ == "__main__":
    main()
