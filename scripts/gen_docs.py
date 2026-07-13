#!/usr/bin/env python3
"""Generate docs/COMMANDS.md from the live argparse parser — single source of
truth, so the command reference can never drift from the actual CLI.

    python3 -m scripts.gen_docs            # write docs/COMMANDS.md
    python3 -m scripts.gen_docs --check    # exit 1 if the file is stale
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.cli.main import build_parser  # noqa: E402

DOCS = Path(__file__).parent.parent / "docs" / "COMMANDS.md"


def _subparsers_action(parser):
    for a in parser._actions:
        if isinstance(a, argparse._SubParsersAction):
            return a
    return None


def _arg_line(action) -> str | None:
    if isinstance(action, argparse._HelpAction) or action.dest == "help":
        return None
    if action.option_strings:
        name = ", ".join(action.option_strings)
    else:
        name = f"<{action.dest}>"
    req = " *(required)*" if getattr(action, "required", False) and action.option_strings else ""
    choices = f" — one of `{', '.join(map(str, action.choices))}`" if action.choices else ""
    help_txt = (action.help or "").strip()
    tail = f" — {help_txt}" if help_txt else ""
    return f"  - `{name}`{req}{tail}{choices}"


def _render_parser(name: str, sub, depth: int = 3) -> list[str]:
    lines = [f"{'#' * depth} `engram {name}`", ""]
    if sub.description:
        lines.append(sub.description.strip())
        lines.append("")
    args = [ln for a in sub._actions if (ln := _arg_line(a))]
    nested = _subparsers_action(sub)
    if args and not nested:
        lines.extend(args)
        lines.append("")
    if nested:
        for child, child_parser in nested.choices.items():
            lines.extend(_render_parser(f"{name} {child}", child_parser, depth + 1))
    return lines


def generate() -> str:
    parser = build_parser()
    sp = _subparsers_action(parser)
    help_by_name = {}
    if sp is not None:
        for ca in sp._choices_actions:
            help_by_name[ca.dest] = (ca.help or "").strip()

    out = [
        "# Engram — Command Reference",
        "",
        "> Auto-generated from the CLI parser by `scripts/gen_docs.py`. "
        "Do not edit by hand; run `python3 -m scripts.gen_docs` after changing commands.",
        "",
        f"All commands are invoked as `engram <command>`. {len(sp.choices) if sp else 0} commands.",
        "",
    ]
    for name, subparser in sorted(sp.choices.items()):
        h = help_by_name.get(name, "")
        out.append(f"### `engram {name}`")
        out.append("")
        if h:
            out.append(h)
            out.append("")
        # arguments + any nested subcommands (add mistake, reflex approve, …)
        args = [ln for a in subparser._actions if (ln := _arg_line(a))]
        nested = _subparsers_action(subparser)
        if args:
            out.extend(args)
            out.append("")
        if nested:
            for child in sorted(nested.choices):
                out.append(f"- **`engram {name} {child}`**")
            out.append("")
    return "\n".join(out).rstrip() + "\n"


def main() -> None:
    content = generate()
    if "--check" in sys.argv:
        current = DOCS.read_text() if DOCS.exists() else ""
        if current != content:
            print("docs/COMMANDS.md is STALE — run: python3 -m scripts.gen_docs", file=sys.stderr)
            sys.exit(1)
        print("docs/COMMANDS.md is up to date.")
        return
    DOCS.write_text(content)
    print(f"Wrote {DOCS} ({content.count('### ')} commands).")


if __name__ == "__main__":
    main()
