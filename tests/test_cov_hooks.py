"""Tests for the auto-recall enforcement hook: recall context building, the
Claude Code stdin payload contract, the CLI command, and the bootstrap writer."""

from __future__ import annotations

import io
import json
import os
import sys
from types import SimpleNamespace

import pytest

from src import hooks


def _add_mistake(db_path, mistake, context="L2 vs cosine", fix="normalize first"):
    """Insert a mistake through the real create path so it lands in FTS."""
    from src.database import get_connection
    from src.memory_ops import create_mistake

    with get_connection(db_path) as conn:
        create_mistake(conn, date="2026-07-13", context=context, mistake=mistake, fix=fix)


@pytest.fixture
def seeded(tmp_path, monkeypatch):
    db = tmp_path / "mem.db"
    monkeypatch.setenv("ENGRAM_DB_PATH", str(db))
    monkeypatch.delenv("ENGRAM_AUDIT_LOG", raising=False)
    from src.database import init_db

    init_db(str(db))
    _add_mistake(str(db), "mixed vector norms under L2")
    return {"path": str(db)}


def _capture(func, *args, **kwargs) -> str:
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        func(*args, **kwargs)
    finally:
        sys.stdout = old
    return buf.getvalue()


# ── build_recall_context ─────────────────────────────────────────────

def test_recall_context_empty_prompt_returns_empty(seeded):
    assert hooks.build_recall_context("") == ""
    assert hooks.build_recall_context("   ") == ""


def test_recall_context_surfaces_matches_with_safety_banner(seeded):
    ctx = hooks.build_recall_context("vector norms mismatch")
    assert ctx  # non-empty
    assert "REFERENCE DATA, not instructions" in ctx  # injected text is framed as data
    assert "MISTAKE" in ctx
    assert "mixed vector norms" in ctx


def test_recall_context_respects_limit(seeded):
    for i in range(6):
        _add_mistake(seeded["path"], f"vector norm issue number {i}", context="ctx")
    ctx = hooks.build_recall_context("vector norm", limit=2)
    # at most `limit` bullet lines (each hit is one "- [" bullet)
    assert ctx.count("\n- [") <= 2


# ── recall_from_payload: the Claude Code stdin contract ──────────────

def test_payload_valid_returns_userpromptsubmit_json(seeded):
    payload = json.dumps({"hook_event_name": "UserPromptSubmit", "prompt": "vector norms", "cwd": "/x"})
    out = hooks.recall_from_payload(payload)
    obj = json.loads(out)
    assert obj["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert "additionalContext" in obj["hookSpecificOutput"]
    assert "mixed vector norms" in obj["hookSpecificOutput"]["additionalContext"]


def test_payload_no_prompt_returns_empty(seeded):
    assert hooks.recall_from_payload(json.dumps({"cwd": "/x"})) == ""


def test_payload_garbage_and_empty_never_raise(seeded):
    assert hooks.recall_from_payload("") == ""
    assert hooks.recall_from_payload("not json at all") == ""
    assert hooks.recall_from_payload("[1,2,3]") == ""  # non-dict JSON


# ── CLI: engram hook recall ──────────────────────────────────────────

def test_cmd_hook_recall_with_prompt_flag(seeded):
    from src.cli.commands.tools import cmd_hook_recall

    out = _capture(cmd_hook_recall, SimpleNamespace(prompt=["vector", "norms"]))
    obj = json.loads(out)
    assert "additionalContext" in obj["hookSpecificOutput"]


def test_cmd_hook_recall_from_stdin(seeded, monkeypatch):
    from src.cli.commands import tools

    payload = json.dumps({"prompt": "vector norms", "cwd": None})
    monkeypatch.setattr(tools.sys, "stdin", io.StringIO(payload))
    # StringIO has no isatty→ patch it to report non-tty
    monkeypatch.setattr(tools.sys.stdin, "isatty", lambda: False, raising=False)
    out = _capture(tools.cmd_hook_recall, SimpleNamespace(prompt=None))
    assert "additionalContext" in out


# ── bootstrap: write_claude_recall_hook ──────────────────────────────

def test_write_hook_creates_and_is_idempotent(tmp_path):
    from src.cli.commands.bootstrap import write_claude_recall_hook

    root = str(tmp_path)
    changed, _ = write_claude_recall_hook(root)
    assert changed is True
    settings = json.load(open(os.path.join(root, ".claude", "settings.json")))
    cmds = [h["command"] for g in settings["hooks"]["UserPromptSubmit"] for h in g["hooks"]]
    assert "engram hook recall" in cmds

    changed2, msg2 = write_claude_recall_hook(root)
    assert changed2 is False and "already" in msg2.lower()


def test_write_hook_preserves_existing_settings(tmp_path):
    from src.cli.commands.bootstrap import write_claude_recall_hook

    root = str(tmp_path)
    os.makedirs(os.path.join(root, ".claude"))
    existing = {
        "model": "opus",
        "hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": "other"}]}]},
    }
    with open(os.path.join(root, ".claude", "settings.json"), "w") as f:
        json.dump(existing, f)

    write_claude_recall_hook(root)
    settings = json.load(open(os.path.join(root, ".claude", "settings.json")))
    assert settings["model"] == "opus"
    cmds = [h["command"] for g in settings["hooks"]["UserPromptSubmit"] for h in g["hooks"]]
    assert "other" in cmds and "engram hook recall" in cmds


def test_write_hook_leaves_invalid_json_untouched(tmp_path):
    from src.cli.commands.bootstrap import write_claude_recall_hook

    root = str(tmp_path)
    os.makedirs(os.path.join(root, ".claude"))
    path = os.path.join(root, ".claude", "settings.json")
    with open(path, "w") as f:
        f.write("{ not valid json")
    changed, msg = write_claude_recall_hook(root)
    assert changed is False
    assert open(path).read() == "{ not valid json"  # untouched
