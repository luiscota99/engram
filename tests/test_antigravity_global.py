"""Tests for ~/.gemini/AGENTS.md global Antigravity snippet."""
from __future__ import annotations

import os

from src.cli.commands.bootstrap import write_global_antigravity_agents_snippet


def test_write_global_creates_file(tmp_path):
    home = str(tmp_path)
    ok, path = write_global_antigravity_agents_snippet(home=home)
    assert ok
    assert path == os.path.join(home, ".gemini", "AGENTS.md")
    text = open(path, encoding="utf-8").read()
    assert "<!-- engram-global:begin -->" in text
    assert "engram search" in text
    assert "<!-- engram-global:end -->" in text


def test_write_global_idempotent_update(tmp_path):
    home = str(tmp_path)
    write_global_antigravity_agents_snippet(home=home)
    p = os.path.join(home, ".gemini", "AGENTS.md")
    open(p, "a", encoding="utf-8").write("\n# user note after\n")
    write_global_antigravity_agents_snippet(home=home)
    text = open(p, encoding="utf-8").read()
    assert text.count("<!-- engram-global:begin -->") == 1
    assert "# user note after" in text
