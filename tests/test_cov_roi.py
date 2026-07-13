"""Tests for the ROI / help-measurement loop: persistent audit toggle,
audit-log summarization, get_roi_report, and the audit/roi CLI + MCP surfaces."""

from __future__ import annotations

import io
import json
import os
from types import SimpleNamespace

import pytest

from src import config


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Point ENGRAM_DB_PATH at a temp dir and clear any audit env override."""
    db = tmp_path / "mem.db"
    monkeypatch.setenv("ENGRAM_DB_PATH", str(db))
    monkeypatch.delenv("ENGRAM_AUDIT_LOG", raising=False)
    from src.database import init_db

    init_db(str(db))
    return {"path": str(db), "dir": str(tmp_path)}


def _capture(func, *args, **kwargs) -> str:
    buf = io.StringIO()
    import sys

    old = sys.stdout
    sys.stdout = buf
    try:
        func(*args, **kwargs)
    finally:
        sys.stdout = old
    return buf.getvalue()


# ── config: persistent store + audit_log_path precedence ──────────────

def test_engram_dir_follows_db_path(store):
    assert config.engram_dir() == store["dir"]


def test_persistent_config_roundtrip(store):
    assert config.get_persistent("audit_enabled") is None
    config.set_persistent("audit_enabled", True)
    assert config.get_persistent("audit_enabled") is True
    # persisted to a real file next to the DB
    assert os.path.isfile(os.path.join(store["dir"], "config.json"))
    # a second key does not clobber the first
    config.set_persistent("other", 5)
    assert config.get_persistent("audit_enabled") is True
    assert config.get_persistent("other") == 5


def test_read_persistent_tolerates_garbage(store):
    with open(os.path.join(store["dir"], "config.json"), "w") as f:
        f.write("{ not json")
    assert config.read_persistent() == {}


def test_audit_log_path_off_by_default(store):
    assert config.audit_log_path() is None


def test_audit_log_path_enabled_uses_default(store):
    config.set_persistent("audit_enabled", True)
    assert config.audit_log_path() == os.path.join(store["dir"], "audit.jsonl")


def test_audit_log_path_env_wins(store, monkeypatch):
    config.set_persistent("audit_enabled", True)
    monkeypatch.setenv("ENGRAM_AUDIT_LOG", "/tmp/explicit.jsonl")
    assert config.audit_log_path() == "/tmp/explicit.jsonl"


# ── search_audit.summarize_audit_log ─────────────────────────────────

def test_summarize_missing_log(store):
    from src.search_audit import summarize_audit_log

    out = summarize_audit_log("/nonexistent/audit.jsonl")
    assert out["searches"] == 0 and out["hit_rate"] is None


def test_summarize_counts_hits_sources_and_top_queries(tmp_path):
    from src.search_audit import summarize_audit_log

    log = tmp_path / "audit.jsonl"
    rows = [
        {"ts": "2026-07-01T00:00:00", "source": "mcp", "query": "vec norm", "result_count": 3},
        {"ts": "2026-07-02T00:00:00", "source": "cli", "query": "vec norm", "result_count": 0},
        {"ts": "2026-07-03T00:00:00", "source": "mcp", "query": "ranking", "result_count": 1},
    ]
    with open(log, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
        f.write("\n")  # blank line tolerated
        f.write("{ broken\n")  # malformed line skipped

    out = summarize_audit_log(str(log))
    assert out["searches"] == 3
    assert out["with_hit"] == 2 and out["zero_result"] == 1
    assert out["hit_rate"] == round(2 / 3, 3)
    assert out["by_source"] == {"mcp": 2, "cli": 1}
    assert out["top_queries"][0] == ("vec norm", 2)
    assert out["first_ts"] == "2026-07-01T00:00:00"
    assert out["last_ts"] == "2026-07-03T00:00:00"


# ── maintenance.get_roi_report verdict branches ──────────────────────

def test_roi_report_verdict_when_auditing_off(store):
    from src.maintenance import get_roi_report

    r = get_roi_report()
    assert r["audit"]["enabled"] is False
    assert "OFF" in r["verdict"] or "auditing is off" in r["verdict"].lower()


def test_roi_report_verdict_when_on_but_no_searches(store):
    config.set_persistent("audit_enabled", True)
    from src.maintenance import get_roi_report

    r = get_roi_report()
    assert r["audit"]["enabled"] is True
    assert r["audit"]["searches"] == 0
    assert "no searches" in r["verdict"].lower()


def test_roi_report_counts_reuse(store):
    from src.database import get_connection

    with get_connection(store["path"]) as conn:
        conn.execute(
            "INSERT INTO skills (name, domain, trigger_desc, workflow, usage_count) "
            "VALUES ('S','engineering','t','w', 3)"
        )
        conn.execute(
            "INSERT INTO skills (name, domain, trigger_desc, workflow, usage_count) "
            "VALUES ('S2','engineering','t','w', 0)"
        )
    from src.maintenance import get_roi_report

    r = get_roi_report()
    assert r["items_used"] == 1
    assert r["used_by_type"]["skill"]["used"] == 1
    assert r["used_by_type"]["skill"]["total"] == 2


# ── CLI: engram audit on/off/status and engram roi ───────────────────

def test_cmd_audit_on_off_status(store):
    from src.cli.commands.maintenance import cmd_audit

    on = _capture(cmd_audit, SimpleNamespace(action="on"))
    assert "enabled" in on.lower()
    assert config.get_persistent("audit_enabled") is True

    status = _capture(cmd_audit, SimpleNamespace(action="status"))
    assert "yes" in status.lower()

    off = _capture(cmd_audit, SimpleNamespace(action="off"))
    assert "disabled" in off.lower()
    assert config.get_persistent("audit_enabled") is False


def test_cmd_roi_runs_and_reports_verdict(store):
    from src.cli.commands.maintenance import cmd_roi

    out = _capture(cmd_roi, SimpleNamespace())
    assert "Engram ROI" in out
    assert "Verdict:" in out


# ── MCP: memory_roi handler ──────────────────────────────────────────

def test_mcp_memory_roi_handler(store):
    from src.mcp.handlers import TOOL_HANDLERS

    out = TOOL_HANDLERS["memory_roi"]({})
    assert isinstance(out, str)
    assert "Engram ROI" in out and "Verdict:" in out
