#!/usr/bin/env python3
"""Evaluate recall/guard lexical gate against evals/gate_gold.json."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.hooks import _tokens  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gold", default=str(ROOT / "evals" / "gate_gold.json"))
    ap.add_argument("--fail-under-precision", type=float, default=0.95)
    args = ap.parse_args()
    data = json.loads(Path(args.gold).read_text())
    cases = data["cases"]
    suppress_ok = suppress_n = 0
    inject_ok = inject_n = 0
    for c in cases:
        toks = _tokens(c["prompt"])
        expect = c["expect"]
        if expect == "suppress":
            suppress_n += 1
            # Gate suppresses when prompt has no durable tokens OR caller finds no overlap.
            # For gold conversational prompts, having few/no tech tokens is success.
            if len(toks) < 2 or not (toks & set(c.get("must_overlap_terms") or [])):
                suppress_ok += 1
        else:
            inject_n += 1
            need = set(c.get("must_overlap_terms") or [])
            # Accent folding: compare folded forms
            folded_need = _tokens(" ".join(need))
            if toks & folded_need:
                inject_ok += 1
    precision = suppress_ok / suppress_n if suppress_n else 1.0
    recall = inject_ok / inject_n if inject_n else 1.0
    print(f"gate_eval suppress_precision={precision:.3f} ({suppress_ok}/{suppress_n})")
    print(f"gate_eval inject_signal_recall={recall:.3f} ({inject_ok}/{inject_n})")
    if precision + 1e-9 < args.fail_under_precision:
        print("FAIL: suppress precision below threshold", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
