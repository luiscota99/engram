"""Validation commands — prove a memory changes behavior (Superpowers TDD-for-skills)."""
from __future__ import annotations

import sys

from ...validation import add_skill_test, run_all_tests, run_skill_test
from ..fmt import fmt_bold, fmt_dim, fmt_header

_ICON = {"validated": "✓", "redundant": "•", "ineffective": "✗", "regressed": "⚠", "untested": "·"}


def cmd_validate(args):
    action = getattr(args, "vaction", None)
    if action == "add":
        try:
            tid = add_skill_test(
                args.type, int(args.id), args.scenario, args.assert_,
                grader=args.grader,
            )
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        print(f"✓ Test #{tid} attached to {args.type} #{args.id}.")
        print(fmt_dim("Run it: engram validate run " + str(tid)))
        return

    if action == "run":
        result = run_skill_test(int(args.id))
        icon = _ICON.get(result["result"], "?")
        print(f"{icon} Test #{result['id']}: {fmt_bold(result['result'].upper())}")
        if "reason" in result:
            print(fmt_dim("  " + result["reason"]))
        elif result["result"] != "validated":
            print(fmt_dim(f"  baseline_passed={result['baseline_passed']} "
                          f"treatment_passed={result['treatment_passed']}"))
            if result["result"] == "redundant":
                print(fmt_dim("  The model already knew this — the memory adds nothing."))
            elif result["result"] == "ineffective":
                print(fmt_dim("  The memory did not fix the behavior — revise it."))
        return

    # default: run all
    print(fmt_header("Running all validation tests…\n"))
    r = run_all_tests()
    if not r["ran"]:
        print(fmt_dim("No validation tests yet. Add one: engram validate add <type> <id> "
                      "--scenario \"...\" --assert \"...\""))
        return
    for res, n in sorted(r["by_result"].items()):
        print(f"  {_ICON.get(res, '?')} {res:<12} {n}")
    validated = r["by_result"].get("validated", 0)
    print(fmt_dim(f"\n{validated}/{r['ran']} memories proven to change behavior."))
