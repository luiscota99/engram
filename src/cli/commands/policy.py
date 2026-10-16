"""``engram policy`` — show or edit the policy file (generate routing + decide pins)."""

from __future__ import annotations

import json
import sys


def add_parser(sub) -> None:
    p = sub.add_parser("policy", help="The policy file: per-job LLM routing (provider/model/mode/privacy/fallback) and decision pins")
    ps = p.add_subparsers(dest="paction")
    p_show = ps.add_parser("show", help="Print the effective policy (defaults filled in) and where it lives")
    p_show.add_argument("--jobs", action="store_true", help="Also print the resolved routing plan per job")
    p_show.set_defaults(func=cmd_policy_show)
    p_set = ps.add_parser("set", help="Set one dotted key, e.g. generate.jobs.extract.model claude-haiku-5-5")
    p_set.add_argument("key", help="Dotted path, e.g. generate.providers.openai.privacy")
    p_set.add_argument("value", help='JSON when it parses (true, 300, ["personal","public"]), else a string')
    p_set.set_defaults(func=cmd_policy_set)
    p_path = ps.add_parser("path", help="Print the policy file path")
    p_path.set_defaults(func=cmd_policy_path)
    p.set_defaults(func=lambda args: p.print_help())


def cmd_policy_show(args) -> None:
    from ...policy import load_policy, policy_path

    print(f"policy file: {policy_path()}")
    print(json.dumps(load_policy(), indent=2, sort_keys=True))
    if getattr(args, "jobs", False):
        from ...llm import get_llm_status

        print("\nresolved jobs:")
        print(json.dumps(get_llm_status().get("policy", {}).get("jobs", {}), indent=2))


def cmd_policy_set(args) -> None:
    from ...policy import set_value

    raw = args.value
    try:
        value = json.loads(raw)
    except ValueError:
        value = raw
    try:
        path = set_value(args.key, value)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"✓ {args.key} = {json.dumps(value)}  ({path})")


def cmd_policy_path(args) -> None:
    from ...policy import policy_path

    print(policy_path())
