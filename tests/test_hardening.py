"""Tests for the July 2026 hardening batch: WAL-correct backups, guarded
inbox transitions, executed-only-on-ok, notify-after-commit, get-or-create
upserts, guard payload clipping, windowed reflex rates, and retention."""

from __future__ import annotations

import hashlib
import json
import os
from unittest.mock import patch

import pytest

from src.database import get_connection, init_db


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = str(tmp_path / "mem.db")
    monkeypatch.setenv("ENGRAM_DB_PATH", path)
    monkeypatch.delenv("ENGRAM_AUDIT_LOG", raising=False)
    init_db(path)
    return path


def _approved_reflex(path, *, name, script, kind="action", read_only=0):
    with get_connection(path) as c:
        cur = c.execute(
            "INSERT INTO skills (name, domain, trigger_desc, workflow) VALUES (?, 'ops', 't', 'w')",
            (f"skill for {name}",),
        )
        sid = cur.lastrowid
        cur = c.execute(
            """INSERT INTO reflexes
               (skill_id, name, description, script, interpreter, kind, read_only,
                approved_at, approved_hash)
               VALUES (?, ?, 'd', ?, 'bash', ?, ?, datetime('now'), ?)""",
            (sid, name, script, kind, read_only,
             hashlib.sha256(script.encode()).hexdigest()),
        )
        return cur.lastrowid


# ── backups are WAL-correct ──────────────────────────────────────────

def test_pre_migration_backup_includes_wal_content(db, tmp_path):
    """Rows committed but still in the -wal file must reach the backup.

    A second connection stays open (as concurrent hooks would be) so closing
    the writer doesn't checkpoint the WAL away before the backup runs.
    """
    import sqlite3

    from src.migrations import backup_before_migration

    # Writer stays OPEN during the backup — close would checkpoint the WAL
    # into the main file and dissolve the very condition under test. This is
    # the live shape: another session's hook mid-write while a migration runs.
    writer = sqlite3.connect(db)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute(
            "INSERT INTO mistakes (date, context, mistake, fix) "
            "VALUES ('2026-07-17', 'ctx', 'wal resident row', 'f')"
        )
        writer.commit()
        assert os.path.exists(db + "-wal")  # the committed row lives in the -wal

        backup = backup_before_migration(db, 99)
        assert backup

        bconn = sqlite3.connect(backup)
        count = bconn.execute(
            "SELECT COUNT(*) FROM mistakes WHERE mistake = 'wal resident row'"
        ).fetchone()[0]
        bconn.close()
        assert count == 1  # shutil.copy2 of the main file would report 0 here
    finally:
        writer.close()


# ── inbox: guarded transitions + executed-only-on-ok ─────────────────

def test_decide_run_failure_stays_approved(db):
    from src.inbox import decide, file_item

    rid = _approved_reflex(db, name="failer", script="exit 1")
    item = file_item(
        kind="decision", severity="high", title="do the thing?",
        proposed_reflex_id=rid, db_path=db, notify=False,
    )
    result = decide(item, "approve", run=True, db_path=db)
    assert result["run_failed"] is True
    assert result["status"] == "approved"  # never claims 'executed' on failure
    with get_connection(db) as c:
        status = c.execute("SELECT status FROM inbox WHERE id = ?", (item,)).fetchone()["status"]
    assert status == "approved"


def test_decide_run_success_marks_executed(db):
    from src.inbox import decide, file_item

    rid = _approved_reflex(db, name="oker", script="echo done")
    item = file_item(
        kind="decision", severity="high", title="do it?",
        proposed_reflex_id=rid, db_path=db, notify=False,
    )
    result = decide(item, "approve", run=True, db_path=db)
    assert result["status"] == "executed"


def test_decide_claims_item_atomically(db):
    from src.inbox import decide, file_item

    item = file_item(kind="alert", severity="info", title="t", db_path=db, notify=False)
    decide(item, "acknowledge", db_path=db)
    with pytest.raises(ValueError, match="already"):
        decide(item, "approve", db_path=db)


def test_file_item_dedup_via_partial_unique_index(db):
    from src.inbox import file_item

    first = file_item(kind="alert", severity="info", title="t",
                      finding_key="k1", db_path=db, notify=False)
    second = file_item(kind="alert", severity="info", title="t again",
                       finding_key="k1", db_path=db, notify=False)
    assert first is not None
    assert second is None  # OR IGNORE against idx_inbox_open_finding
    # resolving the item frees the key for a future filing
    from src.inbox import decide

    decide(first, "acknowledge", db_path=db)
    third = file_item(kind="alert", severity="info", title="t3",
                      finding_key="k1", db_path=db, notify=False)
    assert third is not None


# ── monitor alerts notify AFTER the write transaction commits ────────

def test_monitor_alert_notifies_after_commit(db):
    from src.reflex import run_reflex

    rid = _approved_reflex(db, name="watcher", script="exit 1", kind="monitor")
    with patch("src.inbox.maybe_notify") as notify:
        # If notify ran inside the open transaction, its bookkeeping INSERT
        # would deadlock (observed live pre-fix); here we just assert the
        # dispatch happens and the alert row committed first.
        def assert_committed(*a, **kw):
            with get_connection(db) as c:
                n = c.execute(
                    "SELECT COUNT(*) FROM inbox WHERE finding_key = 'monitor:watcher'"
                ).fetchone()[0]
            assert n == 1  # visible from a fresh connection → committed

        notify.side_effect = assert_committed
        run_reflex(rid, db_path=db)
    assert notify.call_count == 1
    # deduped second fire → no second notification
    with patch("src.inbox.maybe_notify") as notify2:
        run_reflex(rid, db_path=db)
    assert notify2.call_count == 0


# ── get-or-create paths are upsert-shaped ────────────────────────────

def test_ensure_tag_and_project_idempotent(db):
    from src.database import ensure_tag, get_or_create_project

    with get_connection(db) as c:
        t1 = ensure_tag(c, "Alpha")
        t2 = ensure_tag(c, "alpha")  # normalized duplicate
    assert t1 == t2
    p1 = get_or_create_project("/tmp/some-proj", db_path=db)
    p2 = get_or_create_project("/tmp/some-proj", db_path=db)
    assert p1["id"] == p2["id"]


# ── guard payload clip ───────────────────────────────────────────────

def test_guard_query_fallback_is_clipped():
    from src.hooks import _guard_query_from_tool_input

    huge = {"cells": ["x" * 100_000]}  # no known keys → stringified fallback
    q = _guard_query_from_tool_input("NotebookEdit", huge)
    assert len(q) <= 400


# ── windowed reflex success rate ─────────────────────────────────────

def test_success_rate_windows_out_old_failures(db):
    from src.reflex import SUCCESS_RATE_WINDOW, get_reflex_success_rates

    rid = _approved_reflex(db, name="repaired", script="echo ok")
    with get_connection(db) as c:
        for _ in range(10):  # ancient failures
            c.execute(
                "INSERT INTO reflex_runs (reflex_id, started_at, duration_ms, status) "
                "VALUES (?, datetime('now','-30 days'), 5, 'exit_1')", (rid,),
            )
        for _ in range(SUCCESS_RATE_WINDOW):  # repaired: recent successes
            c.execute(
                "INSERT INTO reflex_runs (reflex_id, started_at, duration_ms, status) "
                "VALUES (?, datetime('now'), 5, 'ok')", (rid,),
            )
    rate = get_reflex_success_rates(db_path=db)[rid]
    assert rate["rate"] == 1.0  # all-time would be 0.5 and re-flag forever


# ── retention ────────────────────────────────────────────────────────

def test_retention_prunes_operational_tables_only(db):
    from src.checkpoint import upsert_checkpoint
    from src.maintenance import run_retention

    rid = _approved_reflex(db, name="noisy", script="echo x")
    with get_connection(db) as c:
        for _ in range(60):
            c.execute(
                "INSERT INTO reflex_runs (reflex_id, started_at, duration_ms, status) "
                "VALUES (?, datetime('now'), 1, 'ok')", (rid,),
            )
        c.execute(
            "INSERT INTO inbox (kind, severity, title, status, decided_at) "
            "VALUES ('alert', 'info', 'old decided', 'acknowledged', datetime('now','-120 days'))"
        )
        # memories must never be pruned
        c.execute(
            "INSERT INTO mistakes (date, context, mistake, fix) "
            "VALUES ('2020-01-01', 'c', 'ancient but precious', 'f')"
        )
    upsert_checkpoint("/tmp/p", "old-sess", last_summary="s", db_path=db)
    with get_connection(db) as c:
        c.execute("UPDATE checkpoints SET updated_at = datetime('now','-120 days')")

    counts = run_retention(db_path=db)
    assert counts["reflex_runs"] == 10  # 60 → keep 50
    assert counts["checkpoints"] == 1
    assert counts["inbox"] == 1
    with get_connection(db) as c:
        assert c.execute("SELECT COUNT(*) FROM mistakes").fetchone()[0] == 1


def test_audit_log_rotation(tmp_path, monkeypatch):
    from src import search_audit

    log = tmp_path / "audit.jsonl"
    monkeypatch.setenv("ENGRAM_AUDIT_LOG", str(log))
    log.write_text(json.dumps({"q": "x"}) + "\n")
    monkeypatch.setattr(search_audit, "AUDIT_ROTATE_BYTES", 4)  # tiny cap
    assert search_audit.rotate_audit_log_if_needed() is True
    assert (tmp_path / "audit.jsonl.1").exists()
    assert not log.exists()
    # below cap → no rotation
    log.write_text("{}")
    monkeypatch.setattr(search_audit, "AUDIT_ROTATE_BYTES", 10_000)
    assert search_audit.rotate_audit_log_if_needed() is False
