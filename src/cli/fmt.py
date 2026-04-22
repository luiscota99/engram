"""ANSI formatting helpers shared across CLI command modules."""
from __future__ import annotations


def fmt_header(text: str) -> str:
    return f"\033[1;36m{text}\033[0m"


def fmt_type(t: str) -> str:
    colors = {"mistake": "31", "pattern": "33", "skill": "32", "conversation": "34"}
    code = colors.get(t, "37")
    return f"\033[1;{code}m[{t.upper()}]\033[0m"


def fmt_dim(text: str) -> str:
    return f"\033[2m{text}\033[0m"


def fmt_bold(text: str) -> str:
    return f"\033[1m{text}\033[0m"
