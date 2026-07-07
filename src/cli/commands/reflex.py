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
            runs = f"{r['run_count']} runs, last: {r['last_status'] or '—'}"
            print(f"  #{r['id']} {fmt_bold(r['name'])}  [{state}]  {runs}")
        return

    if action == "approve":
        try:
            res = approve_reflex(int(args.id))
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        print(f"✓ Approved reflex '{res['name']}' (hash {res['approved_hash'][:12]}…).")
        print(fmt_dim("It is now exposed as MCP tool `reflex_" + res["name"] + "`."))
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
