"""The scheduled self-check now watches structural integrity — the drift the
old monitor missed (failed embeddings never retried, FTS/vector drift, orphans).
"""

from __future__ import annotations

import pytest

from src.database import get_connection, init_db
from src.doctor import integrity_report
from src.maintenance import run_self_check
from src.memory_ops import create_mistake


@pytest.fixture
def db(tmp_path, monkeypatch):
    """A DB whose indexes are consistent regardless of whether an embedding
    backend is reachable. CI has no Ollama, so create_mistake indexes FTS but
    can't embed — we reconcile vec_memory to FTS explicitly so the baseline is
    genuinely clean (0 drift) and the drift-injection tests are deterministic."""
    import json

    from src.database import get_vec_dimension

    path = str(tmp_path / "mem.db")
    monkeypatch.setenv("ENGRAM_DB_PATH", path)
    init_db(path)
    with get_connection(path) as conn:
        create_mistake(conn, date="2026-07-13", context="c", mistake="m", fix="f")
        dim = get_vec_dimension(conn=conn)
        for row in conn.execute("SELECT rowid FROM memory_fts").fetchall():
            has_vec = conn.execute(
                "SELECT rowid FROM vec_memory WHERE rowid = ?", (row["rowid"],)
            ).fetchone()
            if not has_vec:
                conn.execute(
                    "INSERT INTO vec_memory(rowid, embedding) VALUES (?, ?)",
                    (row["rowid"], json.dumps([0.001] * dim)),
                )
        conn.execute("UPDATE embedding_status SET status = 'ready'")
    return path


# ── integrity_report: structured, side-effect-free ──────────────────

def test_integrity_report_clean_db_is_all_zero(db):
    r = integrity_report(db_path=db)
    assert r["fts_drift"] == 0
    assert r["vec_drift"] == 0
    assert r["failed_embeddings"] == 0
    assert r["orphaned_status"] == 0
    assert r["orphaned_tags"] == 0


def test_integrity_report_detects_vec_drift_and_failed(db):
    with get_connection(db) as conn:
        conn.execute("UPDATE embedding_status SET status='failed' WHERE rowid IN "
                     "(SELECT rowid FROM embedding_status LIMIT 1)")
        conn.execute("DELETE FROM vec_memory")  # every FTS row now lacks a vector
    r = integrity_report(db_path=db)
    assert r["vec_drift"] >= 1
    assert r["failed_embeddings"] == 1


def test_integrity_report_detects_orphaned_status(db):
    with get_connection(db) as conn:
        conn.execute(
            "INSERT INTO embedding_status (fts_rowid, item_type, item_id, status) "
            "VALUES (99999, 'mistake', 12345, 'pending')"
        )
    r = integrity_report(db_path=db)
    assert r["orphaned_status"] >= 1


# ── run_self_check surfaces integrity findings to the inbox ──────────

def test_self_check_files_integrity_when_drift_present(db):
    with get_connection(db) as conn:
        conn.execute("UPDATE embedding_status SET status='failed' WHERE rowid IN "
                     "(SELECT rowid FROM embedding_status LIMIT 1)")
        conn.execute("DELETE FROM vec_memory")
    filed = run_self_check(db_path=db)["filed"]
    assert "integrity:vector-drift" in filed


def test_self_check_no_integrity_item_when_clean(db):
    filed = run_self_check(db_path=db)["filed"]
    assert not any(f.startswith("integrity:") for f in filed)


def test_self_check_integrity_is_idempotent(db):
    with get_connection(db) as conn:
        conn.execute("DELETE FROM vec_memory")
    first = run_self_check(db_path=db)["filed"]
    assert "integrity:vector-drift" in first
    # second run must not re-file the same open finding (finding_key dedup)
    second = run_self_check(db_path=db)["filed"]
    assert "integrity:vector-drift" not in second
