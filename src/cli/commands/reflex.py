"""CLI commands for reflexes — promote skills to executable, approved scripts."""
from __future__ import annotations

import sys

from ...reflex import (
    approve_reflex,
    list_reflexes,
    promote_skill,
    run_reflex,
)
from ..fmt import fmt_bold, fmt_dim, fmt_header


def cmd_route(args):
    """Action-ladder lookup: cheapest correct way to do a task."""
    from ...router import route_task

    task = " ".join(args.task)
    result = route_task(task)
    print(fmt_header(f"Route: {result['rung'].upper()}\n"))
    print(result["text"])


def cmd_promote(args):
    """Draft a reflex from a proven skill (inert until `engram reflex approve`)."""
    try:
        result = promote_skill(int(args.skill_id))
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    print(fmt_header(f"Drafted reflex #{result['id']} '{result['name']}' ({result['drafted_by']})\n"))
    print(result["script"])
    print()
    print(fmt_dim("Review the script above. When it's correct:"))
    print(fmt_dim(f"  engram reflex approve {result['id']}"))
    print(fmt_dim("Nothing runs until you approve it."))


def cmd_reflex(args):
    action = args.action
    if action == "list":
        rows = list_reflexes()
        if not rows:
            print(fmt_dim("No reflexes yet. Promote a skill: engram promote <skill_id>"))
            return
        print(fmt_header("Reflexes\n"))
        for r in rows:
            state = "approved" if r["approved_at"] else fmt_dim("draft (unapproved)")
            safety = "read-only" if r.get("read_only") else "mutating"
            kind = r.get("kind") or "action"
            runs = f"{r['run_count']} runs, last: {r['last_status'] or '—'}"
            print(f"  #{r['id']} {fmt_bold(r['name'])}  [{state}, {kind}/{safety}]  {runs}")
        return

    if action == "approve":
        read_only = True if getattr(args, "read_only", False) else (
            False if getattr(args, "mutating", False) else None
        )
        try:
            res = approve_reflex(int(args.id), read_only=read_only)
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        ro = " [read-only: runs without confirmation]" if read_only else ""
        print(f"✓ Approved reflex '{res['name']}' (hash {res['approved_hash'][:12]}…){ro}.")
        print(fmt_dim("It is now exposed as MCP tool `reflex_" + res["name"] + "`."))
        if read_only is None:
            print(fmt_dim("Mutating by default — agents get an elicitation confirmation. "
                          "Pass --read-only for safe diagnostics."))
        return

    if action == "run":
        params = {}
        for kv in (args.param or []):
            if "=" in kv:
                k, v = kv.split("=", 1)
                params[k.strip()] = v.strip()
        result = run_reflex(int(args.id), params=params)
        if result["ok"]:
            print(f"✓ Reflex '{result['reflex']}' completed.\n")
        else:
            print(f"✗ Reflex failed ({result.get('status', 'error')}): {result.get('error', '')}\n")
        if result.get("output"):
            print(result["output"])
        sys.exit(0 if result["ok"] else 1)
