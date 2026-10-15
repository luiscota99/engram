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


# ── injection echo: automatic helpfulness from citations ─────────────

def test_echo_detected_and_recorded_once(db, tmp_path, monkeypatch):
    import json as _json

    from src.database import get_connection

    audit = tmp_path / "audit.jsonl"
    monkeypatch.setenv("ENGRAM_AUDIT_LOG", str(audit))
    audit.write_text(_json.dumps({
        "source": "recall_inject", "session_id": "sess-e", "kept": 2, "tokens_est": 100,
        "items": [{"item_type": "mistake", "item_id": 26},
                  {"item_type": "skill", "item_id": 75}],
    }) + "\n")
    transcript = _write_transcript(tmp_path, [
        _user("run the benchmark"),
        _assistant("Per Mistake #26, pinning ENGRAM_DB_PATH to a scratch DB first."),
        _assistant("subagent says mistake #75 too", isSidechain=True),  # never counts
    ])

    n = checkpoint.detect_and_record_echoes("sess-e", transcript, db_path=db)
    assert n == 1  # mistake 26 cited; skill 75 only in a sidechain → no echo
    with get_connection(db) as c:
        rows = [dict(r) for r in c.execute(
            "SELECT item_type, item_id, helpful, source FROM retrieval_feedback"
        ).fetchall()]
    assert rows == [{"item_type": "mistake", "item_id": 26, "helpful": 1, "source": "echo"}]

    # idempotent per (item, session): a later turn's Stop re-scan adds nothing
    assert checkpoint.detect_and_record_echoes("sess-e", transcript, db_path=db) == 0


def test_echo_requires_injection_not_just_citation(db, tmp_path, monkeypatch):
    """Citing a memory the hooks never injected is not an echo."""
    import json as _json

    audit = tmp_path / "audit.jsonl"
    monkeypatch.setenv("ENGRAM_AUDIT_LOG", str(audit))
    audit.write_text(_json.dumps({
        "source": "recall_inject", "session_id": "other-session", "kept": 1,
        "tokens_est": 50, "items": [{"item_type": "mistake", "item_id": 26}],
    }) + "\n")
    transcript = _write_transcript(tmp_path, [
        _assistant("Mistake #26 applies here."),
    ])
    assert checkpoint.detect_and_record_echoes("sess-x", transcript, db_path=db) == 0


def test_stop_payload_triggers_echo_pass(db, tmp_path, monkeypatch):
    import json as _json

    from src.database import get_connection

    audit = tmp_path / "audit.jsonl"
    monkeypatch.setenv("ENGRAM_AUDIT_LOG", str(audit))
    audit.write_text(_json.dumps({
        "source": "guard_inject", "session_id": "sess-full", "kept": 1,
        "tokens_est": 40, "items": [{"item_type": "pattern", "item_id": 13}],
    }) + "\n")
    transcript = _write_transcript(tmp_path, [
        _user("check the eval"),
        _assistant("Pattern #13 warns about toy-benchmark confidence; noting the caveat."),
    ])
    checkpoint.checkpoint_from_stop_payload(_json.dumps({
        "session_id": "sess-full", "transcript_path": transcript, "cwd": str(tmp_path),
    }))
    with get_connection(db) as c:
        n = c.execute(
            "SELECT COUNT(*) FROM retrieval_feedback WHERE source='echo' AND item_type='pattern' AND item_id=13"
        ).fetchone()[0]
    assert n == 1


# ── v29 identity: portable key, host, profile (ADR-0009 §3) ─────────────


def test_same_session_on_two_hosts_is_two_rows_and_resume_sees_both(db, tmp_path, monkeypatch):
    monkeypatch.setenv("ENGRAM_PROJECT_KEY", "github.com/acme/widgets")
    checkpoint.upsert_checkpoint("/Users/a/widgets", "sess-1", last_summary="from the mac", host="mac1")
    checkpoint.upsert_checkpoint("/home/a/widgets", "sess-1", last_summary="from wsl", host="wsl1")
    rows = checkpoint.get_checkpoints("/anything", limit=5)
    assert {r["host"] for r in rows} == {"mac1", "wsl1"}
    assert all(r["project_key"] == "github.com/acme/widgets" for r in rows)
    only_mac = checkpoint.get_checkpoints("/anything", limit=5, host="mac1")
    assert [r["last_summary"] for r in only_mac] == ["from the mac"]


def test_resume_is_profile_scoped_by_default(db, monkeypatch):
    monkeypatch.setenv("ENGRAM_PROJECT_KEY", "github.com/acme/widgets")
    checkpoint.upsert_checkpoint("/p", "s-work", last_summary="work stuff", profile="work")
    checkpoint.upsert_checkpoint("/p", "s-home", last_summary="home stuff", profile="default")
    monkeypatch.setenv("ENGRAM_PROFILE", "default")
    assert [r["session_id"] for r in checkpoint.get_checkpoints("/p", limit=5)] == ["s-home"]
    monkeypatch.setenv("ENGRAM_PROFILE", "work")
    assert [r["session_id"] for r in checkpoint.get_checkpoints("/p", limit=5)] == ["s-work"]
    both = checkpoint.get_checkpoints("/p", limit=5, all_profiles=True)
    assert {r["session_id"] for r in both} == {"s-work", "s-home"}
    report = checkpoint.build_resume_report("/p", limit=2, all_profiles=True)
    assert "work stuff" in report and "home stuff" in report


def test_legacy_rows_matched_by_project_path(db, monkeypatch):
    """Rows written before v29 carry project_key = project_path; still found."""
    from src.database import get_connection

    monkeypatch.delenv("ENGRAM_PROJECT_KEY", raising=False)
    with get_connection(db) as conn:
        conn.execute(
            "INSERT INTO checkpoints (project_path, project_key, host, profile, session_id, last_summary) "
            "VALUES (?, ?, 'oldmac', 'default', 'legacy', 'old handoff')",
            (checkpoint._normalize_project("/tmp/legacy-proj"), checkpoint._normalize_project("/tmp/legacy-proj")),
        )
    monkeypatch.setenv("ENGRAM_PROFILE", "default")
    rows = checkpoint.get_checkpoints("/tmp/legacy-proj", limit=3)
    assert [r["session_id"] for r in rows] == ["legacy"]


def test_stop_payload_prefers_last_assistant_message(db, tmp_path, monkeypatch):
    monkeypatch.setenv("ENGRAM_PROJECT_KEY", "github.com/acme/widgets")
    transcript = _write_transcript(tmp_path, [_user("hi"), _assistant("stale tail text")])
    payload = json.dumps({
        "session_id": "s-direct",
        "transcript_path": transcript,
        "cwd": str(tmp_path),
        "last_assistant_message": "fresh final text",
    })
    checkpoint.checkpoint_from_stop_payload(payload, db_path=db)
    (row,) = checkpoint.get_checkpoints(str(tmp_path), limit=1)
    assert row["last_summary"] == "fresh final text"
    assert row["last_prompt"] == "hi"  # prompt still comes from the transcript tail


def test_record_echoes_from_text(db, tmp_path, monkeypatch):
    from src.database import get_connection
    from src.memory_ops import create_mistake

    with get_connection(db) as conn:
        mid = create_mistake(conn, date="2026-01-01", context="c", mistake="m", fix="f")
    log = tmp_path / "audit.jsonl"
    log.write_text(json.dumps({
        "ts": "t", "source": "recall_inject", "session_id": "s-echo",
        "items": [{"item_type": "mistake", "item_id": mid}],
    }) + "\n")
    monkeypatch.setenv("ENGRAM_AUDIT_LOG", str(log))
    assert checkpoint.record_echoes_from_text("s-echo", f"Per [MISTAKE #{mid}], done.", db_path=db) == 1
    assert checkpoint.record_echoes_from_text("s-echo", f"again [MISTAKE #{mid}]", db_path=db) == 0
    assert checkpoint.record_echoes_from_text("other", f"[MISTAKE #{mid}]", db_path=db) == 0
    with get_connection(db) as conn:
        n = conn.execute("SELECT COUNT(*) FROM retrieval_feedback WHERE source = 'echo'").fetchone()[0]
    assert n == 1
