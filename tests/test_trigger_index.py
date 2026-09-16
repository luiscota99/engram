"""Guard fast path (v28): shingle normalization, the probe, single-owner
maintenance, backfill, and the no-embedding proof for the default guard."""

from __future__ import annotations

import pytest

from src import trigger_index as ti
from src.database import get_connection, init_db


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = str(tmp_path / "mem.db")
    monkeypatch.setenv("ENGRAM_DB_PATH", path)
    monkeypatch.delenv("ENGRAM_GUARD_SEMANTIC", raising=False)
    monkeypatch.delenv("ENGRAM_AUDIT_LOG", raising=False)
    init_db(path)
    return path


def _add_mistake(db_path, mistake, context="ctx"):
    from src.memory_ops import create_mistake

    with get_connection(db_path) as conn:
        create_mistake(conn, date="2026-07-31", context=context, mistake=mistake, fix="f")


# ── normalization + shingles ─────────────────────────────────────────

def test_normalize_bilingual_accents_and_case():
    assert ti.normalize("Verificación de PYTEST") == ["verificacion", "de", "pytest"]


def test_shingles_drop_stopword_only_grams():
    grams = ti.shingles("the pytest of the results")
    assert "the pytest" in grams          # mixed gram kept
    assert "of the" not in grams          # stopword-only gram dropped
    assert any("pytest" in g for g in grams)


# ── probe via single-owner write path ────────────────────────────────

def test_create_indexes_triggers_and_probe_matches(db):
    _add_mistake(db, "piped pytest quiet grep passed check results blind")
    with get_connection(db) as conn:
        n = conn.execute("SELECT COUNT(*) FROM trigger_ngrams").fetchone()[0]
        assert n > 5  # shingles were written by index_in_fts, not a trigger
        hits = ti.probe(conn, "pytest -q | grep passed to check results")
        assert hits and hits[0]["item_type"] == "mistake" and hits[0]["hits"] >= 2


def test_probe_requires_multiple_shingle_overlap(db):
    _add_mistake(db, "docker compose network resolution inside containers")
    with get_connection(db) as conn:
        # one shared word ("docker") but no shared 2-gram → silence
        assert ti.probe(conn, "docker ps") == []
        # unrelated command → silence
        assert ti.probe(conn, "ls -la /tmp") == []


def test_delete_item_cleans_triggers(db):
    from src.database import delete_item

    _add_mistake(db, "unique trigger cleanup subject matter")
    with get_connection(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM trigger_ngrams").fetchone()[0] > 0
        delete_item(conn, "mistake", 1)
        assert conn.execute("SELECT COUNT(*) FROM trigger_ngrams").fetchone()[0] == 0


def test_backfill_indexes_existing_items(db):
    with get_connection(db) as conn:
        conn.execute("DELETE FROM trigger_ngrams")  # simulate pre-v28 state
        assert conn.execute("SELECT COUNT(*) FROM trigger_ngrams").fetchone()[0] == 0
    _add_mistake(db, "backfill candidate about embeddings cold start")
    with get_connection(db) as conn:
        conn.execute("DELETE FROM trigger_ngrams")
        n = ti.backfill(conn)
        assert n >= 1
        assert conn.execute("SELECT COUNT(*) FROM trigger_ngrams").fetchone()[0] > 0


# ── the guard itself: fast by default, provably no embedding ─────────

def test_guard_default_path_never_embeds(db, monkeypatch):
    from src import hooks

    _add_mistake(db, "ollama embedding timeout cold start cascade")

    def _boom(*a, **kw):
        raise AssertionError("embedding was called on the guard fast path")

    monkeypatch.setattr("src.embeddings.embed_text", _boom)
    monkeypatch.setattr("src.embeddings.embed_texts", _boom, raising=False)

    warnings = hooks.build_guard_warnings("ollama embedding timeout during reembed")
    assert warnings and "MISTAKE #1" in warnings[0]

    # and silence on unrelated actions — the 99%-fire era is over
    assert hooks.build_guard_warnings("git status") == []


def test_guard_semantic_escape_hatch(db, monkeypatch):
    """ENGRAM_GUARD_SEMANTIC=1 restores the hybrid-search path."""
    from src import hooks

    monkeypatch.setenv("ENGRAM_GUARD_SEMANTIC", "1")
    called = {}

    def fake_search(*a, **kw):
        called["yes"] = True
        return []

    monkeypatch.setattr("src.search.search", fake_search)
    hooks.build_guard_warnings("anything at all here")
    assert called.get("yes")
