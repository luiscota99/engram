"""``engram decide`` — typed decisions: ask, stats, bench, export, policy, outcome."""

from __future__ import annotations

import json
import sys


def add_parser(sub) -> None:
    p = sub.add_parser("decide", help="Typed decisions (choice / score / yes-no): the LLM node's decide interface")
    ds = p.add_subparsers(dest="daction")

    p_ask = ds.add_parser("ask", help="Answer one question over a piece of text (logs the decision)")
    p_ask.add_argument("question", help="worth_remembering | test_passes | is_duplicate | route_rung")
    p_ask.add_argument("text", help="The state to judge")
    p_ask.add_argument("--context", help="Extra context appended to the state")
    p_ask.add_argument("--backend", choices=("haiku", "laya"), help="Force a backend (default: policy)")
    p_ask.set_defaults(func=cmd_decide_ask)

    p_stats = ds.add_parser("stats", help="Volume, backends, latency p50/p95, outcomes, and haiku↔laya agreement")
    p_stats.add_argument("--question", help="Limit to one question")
    p_stats.add_argument("--json", action="store_true", help="Machine-readable")
    p_stats.set_defaults(func=cmd_decide_stats)

    p_bench = ds.add_parser("bench", help="Re-run recent inputs on both backends: latency + agreement (the promotion gate)")
    p_bench.add_argument("question")
    p_bench.add_argument("-n", "--limit", type=int, default=50)
    p_bench.set_defaults(func=cmd_decide_bench)

    p_exp = ds.add_parser("export", help="Write the fine-tuning set (JSONL) for a question from the decisions log")
    p_exp.add_argument("question")
    p_exp.add_argument("--out", required=True, help="Output .jsonl path")
    p_exp.add_argument("--with-outcome-only", action="store_true", help="Only rows that received an outcome signal")
    p_exp.set_defaults(func=cmd_decide_export)

    p_pol = ds.add_parser("policy", help="Show the decision policy, or pin a question to a backend")
    p_pol.add_argument("--set", dest="pin", metavar="question@vN=backend", help="e.g. worth_remembering@v1=laya")
    p_pol.set_defaults(func=cmd_decide_policy)

    p_out = ds.add_parser("outcome", help="Attach the later signal to a logged decision (kept|dismissed|correct|wrong)")
    p_out.add_argument("decision_id", type=int)
    p_out.add_argument("outcome")
    p_out.set_defaults(func=cmd_decide_outcome)

    p.set_defaults(func=lambda args: p.print_help())


def cmd_decide_ask(args) -> None:
    from ...decide import decide

    d = decide(args.question, args.text, context=args.context, backend=args.backend)
    if d is None:
        print("No decision backend could answer (LLM unavailable and laya not installed).", file=sys.stderr)
        sys.exit(1)
    print(json.dumps({
        "id": d.id, "question": d.key, "answer": d.answer, "confidence": round(d.confidence, 3),
        "backend": d.backend, "model": d.model, "latency_ms": round(d.latency_ms, 1),
        "low_confidence": d.low_confidence,
    }, ensure_ascii=False))


def cmd_decide_stats(args) -> None:
    from ...decide import decision_stats

    stats = decision_stats(question=args.question)
    if args.json:
        print(json.dumps(stats, indent=2))
        return
    if not stats:
        print("No decisions logged yet.")
        return
    for key, b in stats.items():
        print(f"{key}: {b['n']} decisions; backends {b['by_backend']}")
        for be, lat in b["latency_ms"].items():
            print(f"  {be:6s} p50 {lat['p50']} ms  p95 {lat['p95']} ms  (n={lat['n']})")
        if b["outcomes"]:
            print(f"  outcomes: {b['outcomes']}")
        agr = b["agreement"]
        if agr["pairs"]:
            print(f"  haiku↔laya agreement: {agr['rate']} over {agr['pairs']} shared inputs")


def cmd_decide_bench(args) -> None:
    from ...decide import bench

    report = bench(args.question, limit=args.limit)
    print(json.dumps(report, indent=2))
    if not report["laya_installed"]:
        print("laya is not installed: pip install 'engram-memory[laya]'", file=sys.stderr)


def cmd_decide_export(args) -> None:
    from ...decide import export_dataset

    n = export_dataset(args.question, args.out, only_with_outcome=args.with_outcome_only)
    print(f"✓ Wrote {n} rows to {args.out}")


def cmd_decide_policy(args) -> None:
    from ...decide import load_policy, policy_path, set_backend

    if args.pin:
        try:
            key, backend = args.pin.split("=", 1)
            path = set_backend(key.strip(), backend.strip())
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        print(f"✓ {key.strip()} → {backend.strip()} (written to {path})")
        return
    print(f"policy file: {policy_path()}")
    print(json.dumps(load_policy(), indent=2))


def cmd_decide_outcome(args) -> None:
    from ...decide import record_outcome

    if record_outcome(args.decision_id, args.outcome, source="cli"):
        print(f"✓ decision #{args.decision_id} ← {args.outcome}")
    else:
        print(f"decision #{args.decision_id} not found", file=sys.stderr)
        sys.exit(1)
