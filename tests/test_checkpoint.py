"""Tests for crash-proof session checkpoints: transcript-tail extraction, the
Stop-hook stdin contract, upsert semantics, the resume report, and both CLI
and MCP resume surfaces (same core — no dual-surface drift)."""

from __future__ import annotations

import io
import json
import subprocess
import sys
from types import SimpleNamespace

import pytest

from src import checkpoint


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "mem.db"
    monkeypatch.setenv("ENGRAM_DB_PATH", str(path))
    from src.database import init_db

    init_db(str(path))
    return str(path)


def _write_transcript(tmp_path, events):
    p = tmp_path / "session.jsonl"
    p.write_text("\n".join(json.dumps(e) for e in events), encoding="utf-8")
    return str(p)


def _user(text):
    return {"type": "user", "message": {"content": text}}


def _assistant(text, **extra):
    return {
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": text}]},
        **extra,
    }


# ── extract_transcript_tail ──────────────────────────────────────────

def test_extract_finds_last_prompt_and_reply(tmp_path):
    path = _write_transcript(tmp_path, [
        _user("first ask"),
        _assistant("first answer"),
        _user("final ask"),
        _assistant("final answer — the handoff"),
    ])
    prompt, summary = checkpoint.extract_transcript_tail(path)
    assert prompt == "final ask"
    assert summary == "final answer — the handoff"


def test_extract_skips_api_errors_sidechains_and_injected_content(tmp_path):
    path = _write_transcript(tmp_path, [
        _user("real ask"),
        _assistant("real handoff"),
        # subagent chatter and injected/system user content must not win
        _assistant("subagent text", isSidechain=True),
        {"type": "user", "message": {"content": "<task-notification>done</task-notification>"}},
        # the spend-limit banner arrives as assistant text — observed live
        _assistant("You've hit your org's monthly spend limit", isApiErrorMessage=True),
    ])
    prompt, summary = checkpoint.extract_transcript_tail(path)
    assert prompt == "real ask"
    assert summary == "real handoff"


def test_extract_missing_file_returns_empty():
    assert checkpoint.extract_transcript_tail("/nonexistent/t.jsonl") == ("", "")


def test_extract_tolerates_garbage_lines(tmp_path):
    p = tmp_path / "bad.jsonl"
    p.write_text('not json\n[1,2]\n' + json.dumps(_assistant("ok")), encoding="utf-8")
    prompt, summary = checkpoint.extract_transcript_tail(str(p))
    assert summary == "ok"
    assert prompt == ""


# ── upsert semantics ─────────────────────────────────────────────────

def test_upsert_bumps_turns_and_keeps_last_nonblank(db):
    checkpoint.upsert_checkpoint(
        "/tmp/projA", "s1",
        last_prompt="fix it", last_summary="fixed; next: CI",
        git_head="abc fix", git_branch="main",
    )
    # a silent tool-only turn must not erase the captured handoff
    checkpoint.upsert_checkpoint("/tmp/projA", "s1")
    cp = checkpoint.get_checkpoints("/tmp/projA")[0]
    assert cp["turn_count"] == 2
    assert cp["last_summary"] == "fixed; next: CI"
    assert cp["git_head"] == "abc fix"


def test_checkpoints_are_scoped_per_project_and_session(db):
    checkpoint.upsert_checkpoint("/tmp/projA", "s1", last_summary="A1")
    checkpoint.upsert_checkpoint("/tmp/projA", "s2", last_summary="A2")
    checkpoint.upsert_checkpoint("/tmp/projB", "s1", last_summary="B1")
    a = checkpoint.get_checkpoints("/tmp/projA", limit=5)
    assert {c["session_id"] for c in a} == {"s1", "s2"}
    assert all(c["last_summary"].startswith("A") for c in a)


def test_checkpoints_never_enter_fts(db):
    """Operational state, not memory: resume rows must not be searchable."""
    from src.database import get_connection

    checkpoint.upsert_checkpoint("/tmp/projA", "s1", last_summary="zzyzx unique handoff")
    with get_connection(db) as conn:
        hits = conn.execute(
            "SELECT * FROM memory_fts WHERE memory_fts MATCH 'zzyzx'"
        ).fetchall()
    assert hits == []


# ── Stop-hook payload contract ───────────────────────────────────────

def test_stop_payload_records_checkpoint(db, tmp_path):
    transcript = _write_transcript(tmp_path, [
        _user("ship the feature"),
        _assistant("shipped; next steps: watch CI"),
    ])
    checkpoint.checkpoint_from_stop_payload(json.dumps({
        "session_id": "sess-stop",
        "transcript_path": transcript,
        "cwd": str(tmp_path),
    }))
    cp = checkpoint.get_checkpoints(str(tmp_path))[0]
    assert cp["session_id"] == "sess-stop"
    assert cp["last_prompt"] == "ship the feature"
    assert "watch CI" in cp["last_summary"]


@pytest.mark.parametrize("payload", ["", "not json", "[]", json.dumps({"cwd": "/tmp"})])
def test_stop_payload_never_raises(db, payload):
    checkpoint.checkpoint_from_stop_payload(payload)  # no session_id → no-op


# ── resume report ────────────────────────────────────────────────────

def test_resume_report_empty_without_checkpoints(db):
    assert checkpoint.build_resume_report("/tmp/never-seen") == ""


def test_resume_report_shows_handoff_and_commits_since(db, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    def git(*argv):
        subprocess.run(
            ["git", "-C", str(repo), *argv], check=True, capture_output=True,
            env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                 "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
                 "HOME": str(tmp_path), "PATH": "/usr/bin:/bin:/usr/local/bin"},
        )
    git("init")
    (repo / "f").write_text("1")
    git("add", "f")
    git("commit", "-m", "first")
    head = subprocess.run(
        ["git", "-C", str(repo), "log", "-1", "--format=%h %s"],
        capture_output=True, text=True,
    ).stdout.strip()

    checkpoint.upsert_checkpoint(
        str(repo), "s1", last_prompt="do it", last_summary="done; next: X",
        git_head=head, git_branch="main",
    )
    (repo / "f").write_text("2")
    git("commit", "-am", "made after the checkpoint")

    report = checkpoint.build_resume_report(str(repo))
    assert "done; next: X" in report
    assert "do it" in report
    assert "made after the checkpoint" in report  # commits since are surfaced


# ── CLI + MCP surfaces share the core ────────────────────────────────

def _capture(func, *args, **kwargs) -> str:
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        func(*args, **kwargs)
    finally:
        sys.stdout = old
    return buf.getvalue()


def test_cli_resume_outputs_report(db):
    from src.cli.commands.tools import cmd_resume

    checkpoint.upsert_checkpoint("/tmp/projA", "s1", last_summary="the handoff text")
    out = _capture(cmd_resume, SimpleNamespace(project="/tmp/projA", count=1))
    assert "the handoff text" in out


def test_cli_resume_explains_when_empty(db):
    from src.cli.commands.tools import cmd_resume

    out = _capture(cmd_resume, SimpleNamespace(project="/tmp/nowhere", count=1))
    assert "No checkpoints" in out


def test_cli_hook_checkpoint_reads_stdin(db, tmp_path, monkeypatch):
    from src.cli.commands.tools import cmd_hook_checkpoint

    transcript = _write_transcript(tmp_path, [_user("q"), _assistant("a")])
    payload = json.dumps(
        {"session_id": "s-cli", "transcript_path": transcript, "cwd": str(tmp_path)}
    )
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    out = _capture(cmd_hook_checkpoint, SimpleNamespace())
    assert out == ""  # Stop hooks stay silent
    assert checkpoint.get_checkpoints(str(tmp_path))[0]["session_id"] == "s-cli"


def test_mcp_memory_resume_matches_cli(db):
    from src.mcp.handlers import handle_memory_resume

    checkpoint.upsert_checkpoint("/tmp/projA", "s1", last_summary="the handoff text")
    assert "the handoff text" in handle_memory_resume({"project_path": "/tmp/projA"})
    assert "No checkpoints" in handle_memory_resume({"project_path": "/tmp/nowhere"})


def test_bootstrap_writes_stop_hook(tmp_path):
    from src.cli.commands.bootstrap import write_claude_checkpoint_hook

    changed, msg = write_claude_checkpoint_hook(str(tmp_path))
    assert changed, msg
    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    stop = settings["hooks"]["Stop"]
    assert stop[0]["hooks"][0]["command"] == "engram hook checkpoint"
    # idempotent
    changed_again, _ = write_claude_checkpoint_hook(str(tmp_path))
    assert not changed_again


# ── milestone handoffs (v26): deliberate briefings vs ambient turns ──

def test_milestone_persists_across_turn_churn(db, tmp_path):
    """A later 'Now running tests...' turn must never erase the briefing."""
    checkpoint.record_milestone(
        str(tmp_path), "s1", "Shipped the fitter; NEXT: label 30 queries then re-fit.",
        db_path=db,
    )
    for i in range(3):  # ambient turns keep churning
        checkpoint.upsert_checkpoint(
            str(tmp_path), "s1",
            last_prompt=f"turn {i}", last_summary=f"Now running step {i}...",
            db_path=db,
        )
    report = checkpoint.build_resume_report(str(tmp_path), db_path=db)
    assert "MILESTONE HANDOFF" in report
    assert "NEXT: label 30 queries" in report
    assert "Now running step 2..." in report  # ambient turn data still shown


def test_milestone_auto_composes_from_git(db, tmp_path, monkeypatch):
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
           "HOME": str(tmp_path), "PATH": "/usr/bin:/bin:/usr/local/bin"}
    subprocess.run(["git", "-C", str(repo), "init"], check=True, capture_output=True, env=env)
    (repo / "f").write_text("1")
    subprocess.run(["git", "-C", str(repo), "add", "f"], check=True, capture_output=True, env=env)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "the milestone commit"],
                   check=True, capture_output=True, env=env)

    summary = checkpoint.record_milestone(str(repo), "s1", None, db_path=db)
    assert "the milestone commit" in summary
    assert "Milestone on" in summary


def test_cli_handoff_records_and_prints(db, tmp_path, monkeypatch):
    import io
    import sys
    from types import SimpleNamespace

    from src.cli.commands.tools import cmd_handoff

    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    cmd_handoff(SimpleNamespace(
        project=str(tmp_path), session="s-cli", message="Briefing: do X next."
    ))
    assert "Milestone handoff recorded" in buf.getvalue()
    cps = checkpoint.get_checkpoints(str(tmp_path), db_path=db)
    assert cps[0]["milestone_summary"] == "Briefing: do X next."


def test_bootstrap_writes_precompact_hook(tmp_path):
    from src.cli.commands.bootstrap import write_claude_precompact_hook

    changed, _ = write_claude_precompact_hook(str(tmp_path))
    assert changed
    import json as _json

    settings = _json.loads((tmp_path / ".claude" / "settings.json").read_text())
    cmds = [h["command"] for g in settings["hooks"]["PreCompact"] for h in g["hooks"]]
    assert "engram hook checkpoint" in cmds
