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


# ── benchmark instrument gate ────────────────────────────────────────

def test_retrieval_benchmark_refuses_default_db(tmp_path):
    """Without ENGRAM_DB_PATH the bench would score labeled queries against
    the user's real memory DB and report a meaningless number (observed live:
    R@5=0.14 masquerading as a retrieval regression). It must refuse."""
    import subprocess
    import sys as _sys

    env = {k: v for k, v in os.environ.items() if k != "ENGRAM_DB_PATH"}
    env["PYTHONPATH"] = "."
    proc = subprocess.run(
        [_sys.executable, "benchmarks/engram_retrieval_bench.py"],
        capture_output=True, text=True, env=env, timeout=60,
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    )
    assert proc.returncode == 2
    assert "ENGRAM_DB_PATH" in proc.stdout


# ── self-improvement loop: self-check proposes labeling and re-fits ──

def test_self_check_proposes_labeling_when_unlabeled_accumulate(db, tmp_path, monkeypatch):
    import json as _json

    from src.maintenance import SELF_CHECK_UNLABELED_MIN, run_self_check

    audit = tmp_path / "audit.jsonl"
    lines = [
        _json.dumps({"source": "cli", "query": f"real unlabeled question number {i} here"})
        for i in range(SELF_CHECK_UNLABELED_MIN + 2)
    ]
    audit.write_text("\n".join(lines), encoding="utf-8")
    monkeypatch.setenv("ENGRAM_AUDIT_LOG", str(audit))

    run_self_check(db_path=db)
    with get_connection(db) as c:
        row = c.execute(
            "SELECT title FROM inbox WHERE finding_key = 'bench:unlabeled-queries'"
        ).fetchone()
    assert row is not None
    assert "ready to label" in row["title"]


def test_self_check_no_labeling_proposal_below_threshold(db, tmp_path, monkeypatch):
    import json as _json

    from src.maintenance import run_self_check

    audit = tmp_path / "audit.jsonl"
    audit.write_text(
        _json.dumps({"source": "cli", "query": "just one lonely real question"}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ENGRAM_AUDIT_LOG", str(audit))
    run_self_check(db_path=db)
    with get_connection(db) as c:
        n = c.execute(
            "SELECT COUNT(*) FROM inbox WHERE finding_key = 'bench:unlabeled-queries'"
        ).fetchone()[0]
    assert n == 0


# ── restore path + delete_item deep cleanup + soft-FK invariants ─────

def test_restore_roundtrip_with_pre_restore_snapshot(db, tmp_path):
    from src.backup import restore_database
    from src.migrations import backup_before_migration

    with get_connection(db) as c:
        c.execute("INSERT INTO mistakes (date, context, mistake, fix) "
                  "VALUES ('2026-07-17','c','the original state','f')")
    backup = backup_before_migration(db, 90)

    with get_connection(db) as c:  # diverge after the backup
        c.execute("INSERT INTO mistakes (date, context, mistake, fix) "
                  "VALUES ('2026-07-17','c','made after backup','f')")

    result = restore_database(backup, db_path=db)
    assert result["backup_schema_version"] > 0
    assert result["pre_restore_snapshot"] and os.path.exists(result["pre_restore_snapshot"])

    with get_connection(db) as c:
        titles = {r["mistake"] for r in c.execute("SELECT mistake FROM mistakes").fetchall()}
    assert "the original state" in titles
    assert "made after backup" not in titles  # restored to backup state
    # and the diverged state survives in the snapshot
    import sqlite3

    snap = sqlite3.connect(result["pre_restore_snapshot"])
    n = snap.execute("SELECT COUNT(*) FROM mistakes WHERE mistake='made after backup'").fetchone()[0]
    snap.close()
    assert n == 1


def test_restore_refuses_garbage_and_non_engram(db, tmp_path):
    from src.backup import restore_database

    with pytest.raises(ValueError, match="not found"):
        restore_database(str(tmp_path / "nope.db"), db_path=db)
    junk = tmp_path / "junk.db"
    junk.write_text("this is not sqlite")
    with pytest.raises(ValueError):
        restore_database(str(junk), db_path=db)
    import sqlite3

    empty = tmp_path / "empty.db"
    c = sqlite3.connect(str(empty))
    c.execute("CREATE TABLE t (x)")
    c.commit()
    c.close()
    with pytest.raises(ValueError, match="schema_meta"):
        restore_database(str(empty), db_path=db)


def test_delete_item_cleans_all_soft_fk_tables(db):
    from src.database import delete_item, get_or_create_project, link_item_to_project, pin_item
    from src.doctor import integrity_report
    from src.feedback import add_feedback
    from src.memory_ops import create_mistake
    from src.relations import add_relation
    from src.stability import GOOD, record_event

    with get_connection(db) as c:
        create_mistake(c, date="2026-07-17", context="c", mistake="m1", fix="f")
        create_mistake(c, date="2026-07-17", context="c", mistake="m2", fix="f")
    proj = get_or_create_project("/tmp/p", db_path=db)
    link_item_to_project("mistake", 1, proj["id"], db_path=db)
    pin_item("mistake", 1, db_path=db)
    add_feedback("mistake", 1, helpful=True, db_path=db)
    record_event("mistake", 1, GOOD, db_path=db)
    add_relation("mistake", 1, "mistake", 2, "related", db_path=db)

    with get_connection(db) as c:
        delete_item(c, "mistake", 1)

    clean = integrity_report(db_path=db)
    assert clean["soft_fk_orphans"] == 0
    with get_connection(db) as c:
        assert c.execute("SELECT COUNT(*) FROM memory_relations").fetchone()[0] == 0


def test_integrity_report_counts_soft_fk_orphans(db):
    from src.doctor import integrity_report

    with get_connection(db) as c:
        # orphan rows referencing a memory that never existed
        c.execute("INSERT INTO item_pins (item_type, item_id) VALUES ('mistake', 999)")
        c.execute(
            "INSERT INTO retrieval_feedback (item_type, item_id, helpful) VALUES ('skill', 999, 1)"
        )
    r = integrity_report(db_path=db)
    assert r["soft_fk_orphans"] == 2
