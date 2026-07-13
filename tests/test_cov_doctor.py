"""Coverage tests for src/doctor.py (database diagnostics + repair)."""

import urllib.error
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from src import doctor
from src.database import get_connection


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _urlopen_returning(status=200):
    """Build a urlopen replacement usable as a context manager."""
    resp = MagicMock()
    resp.status = status
    cm = MagicMock()
    cm.__enter__.return_value = resp
    cm.__exit__.return_value = False
    return MagicMock(return_value=cm)


@contextmanager
def _externals(*, urlopen=None, url_error=None, llm_available=True, embed=None):
    """Patch every external boundary doctor touches (Ollama HTTP, LLM, embed)."""
    llm_status = {
        "base_url": "http://llm.local/v1",
        "model": "test-model",
        "audit_model": "audit-m",
        "extract_model": "extract-m",
        "available": llm_available,
        "tasks_enabled": [],
        "api_key_set": True,
    }
    if url_error is not None:
        uo = MagicMock(side_effect=url_error)
    else:
        uo = urlopen if urlopen is not None else _urlopen_returning(200)

    with (
        patch("src.doctor.urllib.request.urlopen", uo),
        patch("src.llm.get_llm_status", return_value=llm_status),
        patch("src.llm.is_llm_available", return_value=llm_available),
        patch("src.embeddings.embed_text", return_value=embed),
    ):
        yield


def _seed_indexed_mistake(conn):
    """Insert a mistake row plus a matching FTS entry (no vec row).

    Keeps core_count == fts_count (no FTS drift) so vector drift can be
    exercised in isolation. Returns the memory_fts rowid.
    """
    cur = conn.execute(
        "INSERT INTO mistakes (date, context, mistake, fix) VALUES (?, ?, ?, ?)",
        ("2026-07-13", "ctx", "did a bad thing", "the fix"),
    )
    mid = cur.lastrowid
    cur = conn.execute(
        "INSERT INTO memory_fts (item_type, item_id, title, content, tags) "
        "VALUES (?, ?, ?, ?, ?)",
        ("mistake", str(mid), "did a bad thing", "the fix", ""),
    )
    conn.commit()
    return cur.lastrowid


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------
def test_fmt_helpers_wrap_in_ansi_codes():
    assert doctor.fmt_header("Hi") == "\033[1m\033[36mHi\033[0m"
    assert doctor.fmt_error("Bad") == "\033[31mBad\033[0m"
    assert doctor.fmt_dim("dim") == "\033[2mdim\033[0m"


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------
def test_all_healthy_clean_db(test_db, capsys):
    with _externals():
        doctor.run_diagnostics(repair=False)
    out = capsys.readouterr().out
    assert "No orphaned tags found." in out
    assert "Lexical index matches core tables perfectly." in out
    assert "Semantic index matches search index perfectly." in out
    assert "Ollama is reachable." in out
    assert "No failed embeddings found." in out
    assert "No orphaned entries found." in out
    assert "LLM Engine: http://llm.local/v1 reachable" in out
    assert "model: test-model" in out
    assert "0 issues found. 0 issues repaired." in out


# ---------------------------------------------------------------------------
# Orphaned tags
# ---------------------------------------------------------------------------
def test_orphaned_tags_detected_no_repair(test_db, capsys):
    conn = test_db["conn"]
    conn.execute("INSERT INTO tags (name) VALUES ('lonely')")
    conn.commit()

    with _externals():
        doctor.run_diagnostics(repair=False)
    out = capsys.readouterr().out
    assert "Found 1 orphaned tags (not linked to any memory)." in out
    assert "Deleted orphaned tags." not in out

    with get_connection(test_db["path"]) as c:
        assert c.execute("SELECT COUNT(*) FROM tags").fetchone()[0] == 1


def test_orphaned_tags_repaired(test_db, capsys):
    conn = test_db["conn"]
    conn.execute("INSERT INTO tags (name) VALUES ('a'), ('b')")
    conn.commit()

    with _externals():
        doctor.run_diagnostics(repair=True)
    out = capsys.readouterr().out
    assert "Found 2 orphaned tags" in out
    assert "Repair: Deleted orphaned tags." in out

    with get_connection(test_db["path"]) as c:
        assert c.execute("SELECT COUNT(*) FROM tags").fetchone()[0] == 0


# ---------------------------------------------------------------------------
# FTS drift
# ---------------------------------------------------------------------------
def test_fts_drift_detected_no_repair(test_db, capsys):
    conn = test_db["conn"]
    # Core row with no FTS entry → core_count(1) != fts_count(0)
    conn.execute(
        "INSERT INTO mistakes (date, context, mistake, fix) VALUES (?, ?, ?, ?)",
        ("2026-07-13", "c", "m", "f"),
    )
    conn.commit()

    with _externals():
        doctor.run_diagnostics(repair=False)
    out = capsys.readouterr().out
    assert "FTS Drift detected: 1 core items, but 0 search index entries." in out
    assert "FTS index rebuilt" not in out


def test_fts_drift_repaired(test_db, capsys):
    conn = test_db["conn"]
    conn.execute(
        "INSERT INTO mistakes (date, context, mistake, fix) VALUES (?, ?, ?, ?)",
        ("2026-07-13", "c", "m", "f"),
    )
    conn.commit()

    # embed None → index_in_fts stays hermetic (no Ollama), FTS still repopulates
    with _externals(embed=None), patch("src.database.embed_text", return_value=None):
        doctor.run_diagnostics(repair=True)
    out = capsys.readouterr().out
    assert "Running FTS Rebuild from core tables" in out
    assert "FTS index rebuilt successfully" in out

    with get_connection(test_db["path"]) as c:
        assert c.execute("SELECT COUNT(*) FROM memory_fts").fetchone()[0] == 1


# ---------------------------------------------------------------------------
# Vector drift
# ---------------------------------------------------------------------------
def test_vector_drift_detected_and_repaired(test_db, capsys):
    conn = test_db["conn"]
    _seed_indexed_mistake(conn)  # fts_count 1, vec_count 0, core matches fts

    with _externals(embed=[0.1] * 768):
        doctor.run_diagnostics(repair=True)
    out = capsys.readouterr().out
    assert "Vector Drift detected: 1 search items, but 0 embeddings." in out
    assert "Generating missing embeddings" in out
    assert "Generated 1 missing embeddings." in out

    with get_connection(test_db["path"]) as c:
        assert c.execute("SELECT COUNT(*) FROM vec_memory").fetchone()[0] == 1


# ---------------------------------------------------------------------------
# Ollama reachability
# ---------------------------------------------------------------------------
def test_ollama_offline_urlerror(test_db, capsys):
    err = urllib.error.URLError("connection refused")
    with _externals(url_error=err):
        doctor.run_diagnostics(repair=False)
    out = capsys.readouterr().out
    assert "Semantic Engine Offline: Could not connect to Ollama" in out
    assert "connection refused" in out
    assert "1 issues found" in out


def test_ollama_non_200_status(test_db, capsys):
    with _externals(urlopen=_urlopen_returning(503)):
        doctor.run_diagnostics(repair=False)
    out = capsys.readouterr().out
    assert "Semantic Engine Error: Ollama returned status 503." in out


def test_ollama_generic_exception(test_db, capsys):
    with _externals(url_error=RuntimeError("boom kaboom")):
        doctor.run_diagnostics(repair=False)
    out = capsys.readouterr().out
    assert "Semantic Engine Offline: boom kaboom" in out


# ---------------------------------------------------------------------------
# LLM engine health
# ---------------------------------------------------------------------------
def test_llm_engine_unreachable(test_db, capsys):
    with _externals(llm_available=False):
        doctor.run_diagnostics(repair=False)
    out = capsys.readouterr().out
    assert "LLM Engine: http://llm.local/v1 not reachable." in out
    assert "Consolidation audit, GC scoring" in out
    assert "Set ENGRAM_LLM_BASE_URL / ENGRAM_LLM_API_KEY to enable." in out


# ---------------------------------------------------------------------------
# Failed embeddings recovery
# ---------------------------------------------------------------------------
def test_failed_embeddings_reset_with_ollama(test_db, capsys):
    rowid = _seed_indexed_mistake(test_db["conn"])
    test_db["conn"].execute(
        "INSERT INTO embedding_status (fts_rowid, item_type, item_id, status, error_message) "
        "VALUES (?, 'mistake', 1, 'failed', 'nope')",
        (rowid,),
    )
    test_db["conn"].commit()

    with _externals(embed=[0.1] * 768):  # urlopen 200 → ollama_available
        doctor.run_diagnostics(repair=True)
    out = capsys.readouterr().out
    assert "Found 1 failed embeddings that can be retried." in out
    assert "Reset 1 failed embeddings to pending." in out
    assert "engram reembed" in out

    with get_connection(test_db["path"]) as c:
        row = c.execute(
            "SELECT status, error_message FROM embedding_status WHERE fts_rowid = ?",
            (rowid,),
        ).fetchone()
        assert row["status"] == "pending"
        assert row["error_message"] is None


def test_failed_embeddings_skip_without_ollama(test_db, capsys):
    rowid = _seed_indexed_mistake(test_db["conn"])
    test_db["conn"].execute(
        "INSERT INTO embedding_status (fts_rowid, item_type, item_id, status) "
        "VALUES (?, 'mistake', 1, 'failed')",
        (rowid,),
    )
    test_db["conn"].commit()

    # Ollama offline → ollama_available False → reset skipped
    with _externals(url_error=urllib.error.URLError("down")):
        doctor.run_diagnostics(repair=True)
    out = capsys.readouterr().out
    assert "Found 1 failed embeddings" in out
    assert "Skipping reset: Ollama is not available." in out

    with get_connection(test_db["path"]) as c:
        status = c.execute(
            "SELECT status FROM embedding_status WHERE fts_rowid = ?", (rowid,)
        ).fetchone()[0]
        assert status == "failed"  # untouched


# ---------------------------------------------------------------------------
# Orphaned embedding_status
# ---------------------------------------------------------------------------
def test_orphaned_embedding_status_repaired(test_db, capsys):
    # fts_rowid 999 has no matching memory_fts row → orphaned
    test_db["conn"].execute(
        "INSERT INTO embedding_status (fts_rowid, item_type, item_id, status) "
        "VALUES (999, 'mistake', 42, 'ready')"
    )
    test_db["conn"].commit()

    with _externals():
        doctor.run_diagnostics(repair=True)
    out = capsys.readouterr().out
    assert "Found 1 orphaned embedding_status entries." in out
    assert "Removed 1 orphaned embedding_status entries." in out

    with get_connection(test_db["path"]) as c:
        assert c.execute("SELECT COUNT(*) FROM embedding_status").fetchone()[0] == 0


# ---------------------------------------------------------------------------
# Performance warning
# ---------------------------------------------------------------------------
def test_performance_warning_large_table(test_db, capsys):
    rows = [
        (f"s{i}", f"title {i}", "2026-07-13", "eng")
        for i in range(10001)
    ]
    test_db["conn"].executemany(
        "INSERT INTO sessions (session_id, title, date, domain) VALUES (?, ?, ?, ?)",
        rows,
    )
    test_db["conn"].commit()

    with _externals():
        doctor.run_diagnostics(repair=False)
    out = capsys.readouterr().out
    assert "The `sessions` table has >10,000 rows (10001)." in out
    assert "CREATE INDEX idx_sessions_domain_date" in out
