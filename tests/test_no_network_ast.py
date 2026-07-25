"""CI invariant: core modules must not hardcode unexpected network hosts."""

from __future__ import annotations

import ast
from pathlib import Path

# Allowed host substrings appearing as string constants in core modules.
_ALLOW = (
    "localhost",
    "127.0.0.1",
    "ollama",
    "openai.com",
    "api.openai.com",
    "huggingface.co",  # optional model pulls for fastembed
    "github.com",
    "example.com",
)


def _string_hosts(tree: ast.AST) -> list[str]:
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            v = node.value
            if "://" in v or v.startswith("http"):
                found.append(v)
    return found


def test_no_unexpected_network_literals_in_core():
    root = Path(__file__).resolve().parents[1] / "src"
    files = [
        root / "config.py",
        root / "embeddings.py",
        root / "llm.py",
        root / "mcp" / "http_server.py",
    ]
    bad = []
    for path in files:
        if not path.is_file():
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        for s in _string_hosts(tree):
            if not any(a in s for a in _ALLOW):
                bad.append((str(path), s))
    assert not bad, f"Unexpected network literals: {bad}"
