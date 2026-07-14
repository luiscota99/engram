"""Typed relationships between memory items (schema v20) — the useful slice of
a knowledge graph without OWL/RDF: closed vocabulary, low friction, auto-edges."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from src import relations
from src.database import get_connection, init_db
from src.memory_ops import create_mistake, create_pattern


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = str(tmp_path / "mem.db")
    monkeypatch.setenv("ENGRAM_DB_PATH", path)
    monkeypatch.delenv("ENGRAM_AUDIT_LOG", raising=False)
    init_db(path)
    with get_connection(path) as conn:
        create_mistake(conn, date="2026-07-13", context="c", mistake="N+1 in loop", fix="batch it")
        create_pattern(conn, name="Batch with JOIN", symptoms="s", root_cause="r", standard_fix="f")
    return path


# ── fresh install has the table (in SCHEMA_SQL, not only migrations) ─

def test_fresh_db_has_relations_table(db):
    with get_connection(db) as conn:
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE name='memory_relations'"
        ).fetchone() is not None


# ── add_relation ─────────────────────────────────────────────────────

def test_add_relation_happy_path(db):
    assert relations.add_relation("mistake", 1, "pattern", 1, "causes", db_path=db) is None
    rels = relations.get_relations("mistake", 1, db_path=db)
    assert len(rels) == 1
    assert rels[0]["relation"] == "causes" and rels[0]["direction"] == "out"
    assert rels[0]["other_type"] == "pattern" and rels[0]["other_title"] == "Batch with JOIN"


def test_add_relation_is_idempotent(db):
    relations.add_relation("mistake", 1, "pattern", 1, "causes", db_path=db)
    relations.add_relation("mistake", 1, "pattern", 1, "causes", db_path=db)
    assert len(relations.get_relations("mistake", 1, db_path=db)) == 1


def test_add_relation_rejects_unknown_relation(db):
    err = relations.add_relation("mistake", 1, "pattern", 1, "enables", db_path=db)
    assert err and "Unknown relation" in err


def test_add_relation_rejects_bad_type(db):
    err = relations.add_relation("widget", 1, "pattern", 1, "related", db_path=db)
    assert err and "Item types" in err


def test_add_relation_rejects_self_link(db):
    err = relations.add_relation("mistake", 1, "mistake", 1, "related", db_path=db)
    assert err and "itself" in err


def test_add_relation_validates_endpoints_exist(db):
    err = relations.add_relation("mistake", 1, "pattern", 999, "related", db_path=db)
    assert err and "No pattern with id 999" in err


def test_get_relations_shows_incoming(db):
    relations.add_relation("mistake", 1, "pattern", 1, "causes", db_path=db)
    incoming = relations.get_relations("pattern", 1, db_path=db)
    assert len(incoming) == 1
    assert incoming[0]["direction"] == "in"
    assert incoming[0]["other_type"] == "mistake"


# ── auto-edge: a merge/supersede records `supersedes` ────────────────

def test_invalidate_records_supersedes_edge(db):
    from src.temporal import invalidate_memory

    with get_connection(db) as conn:
        keeper = create_mistake(conn, date="2026-07-13", context="c", mistake="keeper", fix="f")
        loser = create_mistake(conn, date="2026-07-13", context="c", mistake="loser", fix="f")
    ok = invalidate_memory("mistake", loser, superseded_by=keeper, reason="merge", db_path=db)
    assert ok
    edges = relations.get_relations("mistake", keeper, db_path=db)
    supersedes = [e for e in edges if e["relation"] == "supersedes"]
    assert supersedes and supersedes[0]["other_id"] == loser
    assert supersedes[0]["source"] == "merge"


# ── CLI: link + relations, with type:id parsing and plural aliases ───

def test_cli_link_and_relations(db, capsys):
    from src.cli.commands.memory import cmd_link, cmd_relations

    cmd_link(SimpleNamespace(source="mistake:1", target="pattern:1", relation="causes"))
    assert "Linked" in capsys.readouterr().out

    # plural alias 'mistakes:1' resolves to 'mistake'
    cmd_relations(SimpleNamespace(item="mistakes:1"))
    out = capsys.readouterr().out
    assert "causes" in out and "pattern #1" in out


def test_cli_link_rejects_bad_ref(db):
    from src.cli.commands.memory import cmd_link

    with pytest.raises(SystemExit):
        cmd_link(SimpleNamespace(source="not-a-ref", target="pattern:1", relation="causes"))


# ── MCP: memory_link + read_item attaches relations ──────────────────

def test_mcp_link_and_read_item_attaches_relations(db):
    from src.mcp.handlers import TOOL_HANDLERS

    msg = TOOL_HANDLERS["memory_link"](
        {"from_type": "mistake", "from_id": 1, "to_type": "pattern", "to_id": 1, "relation": "causes"}
    )
    assert "Linked" in msg

    read = TOOL_HANDLERS["memory_read_item"]({"item_type": "mistake", "item_id": 1})
    parsed = json.loads(read)
    assert "relations" in parsed
    assert parsed["relations"][0]["relation"] == "causes"


def test_mcp_link_rejects_unknown_relation(db):
    from src.mcp.handlers import TOOL_HANDLERS

    msg = TOOL_HANDLERS["memory_link"](
        {"from_type": "mistake", "from_id": 1, "to_type": "pattern", "to_id": 1, "relation": "enables"}
    )
    assert msg.startswith("Error:")
