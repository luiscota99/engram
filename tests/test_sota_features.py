"""SOTA competitive features: triggers, as_of, explain, entities, kg, providers."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from src.database import get_connection, get_pinned_items, init_db, pin_item
from src.entities import auto_link_from_text, entities_for_item, extract_entity_candidates
from src.errors import DuplicateBlocked, EngramError
from src.extract_ops import classify_extract_candidate
from src.feedback import add_feedback
from src.hooks import build_guard_warnings
from src.importers import import_mem0_export
from src.memory_ops import create_mistake, create_pattern, create_skill
from src.providers.native import NativeSqliteProvider
from src.search import search
from src.temporal import invalidate_memory, kg_timeline
from src.trigger_index import probe_triggers, reindex_all_triggers


@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    monkeypatch.setenv("ENGRAM_DB_PATH", str(path))
    monkeypatch.setenv("ENGRAM_EMBED_URL", "disabled")
    init_db()
    return str(path)


def test_trigger_guard_fast_path(db, monkeypatch):
    monkeypatch.setenv("ENGRAM_GUARD_FALLBACK", "off")
    with get_connection(db) as conn:
        create_mistake(
            conn,
            date="2026-07-01",
            context="hooks",
            mistake="Forgot busy_timeout under concurrent writers",
            fix="Set busy_timeout",
            tags=["sqlite"],
        )
    reindex_all_triggers(db)
    hits = probe_triggers("Forgot busy_timeout under concurrent writers today", db_path=db)
    assert hits
    warnings = build_guard_warnings(
        "editing about busy_timeout concurrent writers", db_path=db
    )
    assert warnings
    assert "MISTAKE" in warnings[0]


def test_as_of_and_explain(db):
    with get_connection(db) as conn:
        sid = create_skill(
            conn,
            name="Use WAL mode",
            domain="db",
            trigger="sqlite concurrency",
            workflow="enable WAL",
            tags=["sqlite"],
        )
    assert search("WAL mode", limit=5, skip_audit=True, db_path=db)
    invalidate_memory("skill", sid, reason="obsolete", db_path=db)
    remaining = search("WAL mode", limit=5, skip_audit=True, db_path=db)
    assert all(r.get("item_id") != str(sid) and int(r.get("item_id", -1)) != sid for r in remaining)
    assert kg_timeline(f"skill:{sid}", db_path=db)
    explained = search("sqlite", limit=3, explain=True, skip_audit=True, db_path=db)
    # may be empty if only skill matched — add a mistake
    with get_connection(db) as conn:
        create_mistake(
            conn,
            date="2026-07-02",
            context="db",
            mistake="sqlite lock errors without WAL",
            fix="PRAGMA journal_mode=WAL",
            tags=["sqlite"],
        )
    explained = search("sqlite WAL", limit=3, explain=True, skip_audit=True, db_path=db)
    assert explained
    assert any(r.get("score_breakdown") for r in explained)


def test_pin_snapshot_no_fts_join(db):
    with get_connection(db) as conn:
        mid = create_mistake(
            conn,
            date="2026-07-01",
            context="x",
            mistake="Alpha compositing edge case",
            fix="use tinting",
            tags=["graphics"],
        )
    assert pin_item("mistake", mid, db_path=db)
    pins = get_pinned_items(db_path=db)
    assert pins and pins[0]["title"]


def test_feedback_idempotency(db):
    with get_connection(db) as conn:
        mid = create_mistake(
            conn,
            date="2026-07-01",
            context="x",
            mistake="duplicate feedback should not double count",
            fix="use idempotency_key",
            tags=["test"],
        )
    assert add_feedback("mistake", mid, helpful=True, idempotency_key="k1", db_path=db)
    assert add_feedback("mistake", mid, helpful=True, idempotency_key="k1", db_path=db)
    with get_connection(db) as conn:
        n = conn.execute(
            "SELECT COUNT(*) AS c FROM retrieval_feedback WHERE item_id = ?", (mid,)
        ).fetchone()["c"]
    assert n == 1


def test_entities_and_provider(db):
    names = extract_entity_candidates("Use SQLite WAL with busy_timeout in MCP hooks")
    assert names
    with get_connection(db) as conn:
        mid = create_mistake(
            conn,
            date="2026-07-01",
            context="MCP",
            mistake="SQLite busy_timeout missing in hooks",
            fix="set pragma",
            tags=["sqlite"],
        )
        auto_link_from_text("mistake", mid, "SQLite busy_timeout MCP", conn=conn)
    ents = entities_for_item("mistake", mid, db_path=db)
    assert ents
    p = NativeSqliteProvider(db_path=db)
    assert p.search("busy_timeout", limit=3, skip_audit=True)


def test_extract_ops_and_importer(db, tmp_path):
    with get_connection(db) as conn:
        create_pattern(
            conn,
            name="API Parameter Mismatch",
            symptoms="wrong id from client",
            root_cause="client invented id",
            standard_fix="look up from listing",
            tags=["api"],
        )
    op = classify_extract_candidate(
        "API Parameter Mismatch",
        "wrong id from client",
        item_type="pattern",
        db_path=db,
    )
    assert op.op in ("NOOP", "UPDATE", "ADD")
    payload = tmp_path / "mem0.json"
    payload.write_text(json.dumps([{"memory": "Always pin the embed model with keep_alive"}]))
    result = import_mem0_export(payload, db_path=db)
    assert result["added"] >= 1


def test_engram_error_dict():
    err = DuplicateBlocked("near duplicate")
    d = err.to_dict()
    assert d["ok"] is False and d["code"] == "duplicate_blocked"


def test_mcp_public_tool_count():
    from src.mcp.tools_schema import TOOLS_PUBLIC

    assert 10 <= len(TOOLS_PUBLIC) <= 20
    names = {t["name"] for t in TOOLS_PUBLIC}
    assert "memory_kg" in names and "memory_maintain" in names
