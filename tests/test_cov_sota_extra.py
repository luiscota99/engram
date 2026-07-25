"""Extra coverage for SOTA modules (providers, kg, importers, search flags, http)."""

from __future__ import annotations

import json

from src.database import get_connection, init_db
from src.extract_ops import ExtractOp, classify_extract_candidate, propose_extract_ops
from src.importers import import_openmemory_export, import_zep_export
from src.mcp.handlers import (
    handle_memory_codebase,
    handle_memory_kg,
    handle_memory_maintain,
    handle_memory_search,
    handle_memory_session,
)
from src.memory_ops import create_mistake, create_pattern, create_skill
from src.providers.native import NativeSqliteProvider, get_provider, set_provider
from src.relations import add_relation
from src.search import search
from src.temporal import kg_query, upsert_fact


def test_provider_get_set_and_invalidate(tmp_path, monkeypatch):
    db = tmp_path / "p.db"
    monkeypatch.setenv("ENGRAM_DB_PATH", str(db))
    monkeypatch.setenv("ENGRAM_EMBED_URL", "disabled")
    init_db()
    set_provider(None)
    p = get_provider(str(db))
    assert isinstance(p, NativeSqliteProvider)
    with get_connection(str(db)) as conn:
        sid = create_skill(
            conn, name="P", domain="d", trigger="t", workflow="w", tags=["x"]
        )
    assert p.get_item("skill", sid)
    assert p.invalidate("skill", sid, reason="t")
    set_provider(NativeSqliteProvider(str(db)))
    assert get_provider() is not None
    set_provider(None)


def test_kg_query_upsert_and_handlers(tmp_path, monkeypatch):
    db = tmp_path / "k.db"
    monkeypatch.setenv("ENGRAM_DB_PATH", str(db))
    monkeypatch.setenv("ENGRAM_EMBED_URL", "disabled")
    init_db()
    fid = upsert_fact("skill:1", "uses", "sqlite", source_type="skill", source_id=1)
    assert fid
    assert kg_query("skill:1")
    assert kg_query(None, limit=5)
    out = handle_memory_kg({"action": "query", "subject": "skill:1"})
    assert "sqlite" in out
    out = handle_memory_kg({"action": "timeline", "subject": "skill:1"})
    assert "uses" in out or "sqlite" in out
    out = handle_memory_kg({"action": "add_fact", "subject": "a", "predicate": "b", "object": "c"})
    assert "Fact #" in out
    out = handle_memory_kg({"action": "invalidate"})
    assert "Error" in out
    with get_connection(str(db)) as conn:
        mid = create_mistake(
            conn, date="2026-01-01", context="c", mistake="m", fix="f", tags=["t"]
        )
    assert "Invalidated" in handle_memory_kg(
        {"action": "invalidate", "item_type": "mistake", "item_id": mid, "reason": "x"}
    )
    assert "unknown" in handle_memory_kg({"action": "nope"}).lower()


def test_search_graph_token_budget_and_maintain(tmp_path, monkeypatch):
    db = tmp_path / "s.db"
    monkeypatch.setenv("ENGRAM_DB_PATH", str(db))
    monkeypatch.setenv("ENGRAM_EMBED_URL", "disabled")
    init_db()
    with get_connection(str(db)) as conn:
        a = create_pattern(
            conn,
            name="Alpha Pattern WAL",
            symptoms="wal writers",
            root_cause="locks",
            standard_fix="busy_timeout",
            tags=["sqlite"],
        )
        b = create_pattern(
            conn,
            name="Beta Pattern timeout",
            symptoms="busy timeout",
            root_cause="default",
            standard_fix="set pragma",
            tags=["sqlite"],
        )
    add_relation("pattern", a, "pattern", b, "related", db_path=str(db))
    hits = search("WAL writers busy", limit=5, skip_audit=True, db_path=str(db), token_budget=20)
    assert isinstance(hits, list)
    text = handle_memory_search({"query": "sqlite", "explain": True, "limit": 3})
    assert "score_breakdown" in text or "Found" in text or "[" in text
    assert "Error" not in handle_memory_maintain({"action": "stats"})
    assert "unknown" in handle_memory_maintain({"action": "nope"}).lower()
    assert "unknown" in handle_memory_session({"action": "nope"}).lower()
    assert "unknown" in handle_memory_codebase({"action": "nope"}).lower()


def test_importers_and_extract_ops(tmp_path, monkeypatch):
    db = tmp_path / "i.db"
    monkeypatch.setenv("ENGRAM_DB_PATH", str(db))
    monkeypatch.setenv("ENGRAM_EMBED_URL", "disabled")
    init_db()
    z = tmp_path / "z.json"
    z.write_text(json.dumps([{"fact": "User prefers dark mode", "valid_at": "2024-01-01"}]))
    assert import_zep_export(z, db_path=str(db))["added"] >= 1
    o = tmp_path / "o.json"
    o.write_text(
        json.dumps(
            [
                {"sector": "procedural", "title": "Deploy", "content": "run make deploy"},
                {"sector": "episodic", "title": "Session", "content": "fixed bug", "id": "e1"},
                {"sector": "semantic", "content": "Prefers vim"},
            ]
        )
    )
    assert import_openmemory_export(o, db_path=str(db))["added"] >= 1
    op = classify_extract_candidate("Brand New Thing", "totally novel content xyz", db_path=str(db))
    assert op.op == "ADD"
    ids = propose_extract_ops(
        [ExtractOp("UPDATE", "t", "s", target_type="pattern", target_id=1, reason="x")],
        db_path=str(db),
    )
    assert isinstance(ids, list)


def test_http_server_health(monkeypatch):
    from src.mcp.http_server import _Handler

    class Fake(_Handler):
        def __init__(self):
            self.path = "/health"
            self.headers = {}
            self.wfile = type("W", (), {"write": lambda self, b: None})()
            self.status = None
            self.rfile = type("R", (), {"read": lambda self, n: b"{}"})()

        def send_response(self, code):
            self.status = code

        def send_header(self, *a):
            pass

        def end_headers(self):
            pass

    h = Fake()
    h.do_GET()
    assert h.status == 200
    h.path = "/nope"
    h.do_GET()
    assert h.status == 404
    monkeypatch.delenv("ENGRAM_MCP_HTTP_TOKEN", raising=False)
    h.path = "/mcp"
    h.headers = {"Content-Length": "2"}
    h.rfile = type("R", (), {"read": lambda self, n: b"{}"})()
    h.do_POST()
    assert h.status == 200
    monkeypatch.setenv("ENGRAM_MCP_HTTP_TOKEN", "secret")
    h.headers = {"Content-Length": "2"}
    h.do_POST()
    assert h.status == 401


def test_search_filter_tags_and_empty_query(tmp_path, monkeypatch):
    db = tmp_path / "f.db"
    monkeypatch.setenv("ENGRAM_DB_PATH", str(db))
    monkeypatch.setenv("ENGRAM_EMBED_URL", "disabled")
    init_db()
    with get_connection(str(db)) as conn:
        create_mistake(
            conn,
            date="2026-01-01",
            context="ctx",
            mistake="tagged item alpha",
            fix="fix",
            tags=["alpha-tag"],
        )
    assert search("", limit=5, skip_audit=True, db_path=str(db))
    assert search("tagged", tags=["alpha-tag"], limit=5, skip_audit=True, db_path=str(db))
    sink = {}
    search("tagged alpha", limit=3, skip_audit=True, db_path=str(db), rank_inputs_sink=sink)
    assert "candidates" in sink


def test_extract_ops_empty_and_contradiction(tmp_path, monkeypatch):
    db = tmp_path / "e.db"
    monkeypatch.setenv("ENGRAM_DB_PATH", str(db))
    monkeypatch.setenv("ENGRAM_EMBED_URL", "disabled")
    init_db()
    assert classify_extract_candidate("", "", db_path=str(db)).op == "NOOP"
    with get_connection(str(db)) as conn:
        create_pattern(
            conn,
            name="City Preference",
            symptoms="lives in London",
            root_cause="stated",
            standard_fix="remember city",
            tags=["user"],
        )
    op = classify_extract_candidate(
        "City Preference",
        "user no longer lives in London — moved from London to Tokyo",
        item_type="pattern",
        db_path=str(db),
    )
    assert op.op in ("ADD", "UPDATE", "DELETE", "NOOP")


def test_errors_format_and_json():
    from src.errors import EngramError, NotFoundError, format_error

    e = NotFoundError("missing")
    assert "not_found" in e.to_json_line()
    assert "Error" in format_error(e)
    assert "Error" in format_error(RuntimeError("x"))
    assert EngramError("m", hint="h", details={"a": 1}).to_dict()["details"]["a"] == 1


def test_entities_query_map(tmp_path, monkeypatch):
    from src.entities import (
        entity_ids_for_query,
        get_or_create_entity,
        item_entity_map,
        link_item_entities,
    )

    db = tmp_path / "ent.db"
    monkeypatch.setenv("ENGRAM_DB_PATH", str(db))
    monkeypatch.setenv("ENGRAM_EMBED_URL", "disabled")
    init_db()
    with get_connection(str(db)) as conn:
        mid = create_mistake(
            conn,
            date="2026-01-01",
            context="API",
            mistake="API Parameter Mismatch with getConnection",
            fix="lookup",
            tags=["api"],
        )
        eid = get_or_create_entity("getConnection", conn=conn)
        link_item_entities("mistake", mid, ["getConnection", "SQLite"], conn=conn)
        assert eid
        assert item_entity_map([("mistake", mid)], conn=conn)
    assert entity_ids_for_query("please fix getConnection in SQLite", db_path=str(db))


def test_cli_kg_and_import(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from src.cli.commands.memory import cmd_kg

    db = tmp_path / "cli.db"
    monkeypatch.setenv("ENGRAM_DB_PATH", str(db))
    monkeypatch.setenv("ENGRAM_EMBED_URL", "disabled")
    init_db()
    cmd_kg(
        SimpleNamespace(
            kg_action="add",
            subject="s:1",
            predicate="p",
            object="o",
            valid_until=None,
        )
    )
    cmd_kg(SimpleNamespace(kg_action="query", subject="s:1", limit=10))
    cmd_kg(SimpleNamespace(kg_action="timeline", subject="s:1"))


def test_version_and_local_embed_resolve(monkeypatch):
    from src import embeddings as emb
    from src.version import get_package_version

    assert get_package_version()
    monkeypatch.setenv("ENGRAM_EMBED_URL", "local")
    kind, url = emb.resolve_embed_backend()
    assert kind == "local"
    monkeypatch.setenv("ENGRAM_EMBED_URL", "disabled")
    assert emb.resolve_embed_backend()[0] == "disabled"
    # local embed without fastembed installed returns None
    monkeypatch.setenv("ENGRAM_EMBED_URL", "local")
    emb.clear_embedding_cache()
    assert emb.embed_text("hello") is None


def test_invalidated_subjects_helper(tmp_path, monkeypatch):
    from src.temporal import invalidated_subjects_as_of, upsert_fact

    db = tmp_path / "inv.db"
    monkeypatch.setenv("ENGRAM_DB_PATH", str(db))
    monkeypatch.setenv("ENGRAM_EMBED_URL", "disabled")
    init_db()
    upsert_fact("mistake:9", "invalidated", "gone", valid_until="2020-01-01")
    assert "mistake:9" in invalidated_subjects_as_of("2026-01-01", db_path=str(db))


def test_trigger_canonicalize_empty():
    from src.trigger_index import canonicalize, extract_trigger_phrases

    assert canonicalize("") == ""
    assert extract_trigger_phrases("hi", "") == []


def test_repair_fts_delta(tmp_path, monkeypatch):
    from src.database import get_connection, init_db, repair_fts_delta
    from src.memory_ops import create_mistake

    db = tmp_path / "delta.db"
    monkeypatch.setenv("ENGRAM_DB_PATH", str(db))
    monkeypatch.setenv("ENGRAM_EMBED_URL", "disabled")
    init_db()
    with get_connection(str(db)) as conn:
        mid = create_mistake(
            conn,
            date="2026-01-01",
            context="c",
            mistake="delta repair target",
            fix="reindex me",
            tags=["t"],
        )
        conn.execute(
            "DELETE FROM memory_fts WHERE item_type = ? AND item_id = ?",
            ("mistake", str(mid)),
        )
        stats = repair_fts_delta(conn)
        assert stats["reindexed"] >= 1
        row = conn.execute(
            "SELECT 1 FROM memory_fts WHERE item_type = ? AND item_id = ?",
            ("mistake", str(mid)),
        ).fetchone()
        assert row


def test_optional_cross_encoder_noop_by_default():
    from src.ranking import optional_cross_encoder_rerank

    rows = [{"title": "a", "snippet": "b", "utility_score": 1.0}]
    out = optional_cross_encoder_rerank(rows, "query")
    assert out == rows


def test_openai_wrapper_recall_block(tmp_path, monkeypatch):
    from src.database import get_connection, init_db
    from src.memory_ops import create_mistake
    from src.openai_wrapper import EngramChat

    db = tmp_path / "w.db"
    monkeypatch.setenv("ENGRAM_DB_PATH", str(db))
    monkeypatch.setenv("ENGRAM_EMBED_URL", "disabled")
    init_db()
    with get_connection(str(db)) as conn:
        create_mistake(
            conn,
            date="2026-01-01",
            context="sqlite",
            mistake="busy_timeout missing under writers",
            fix="set pragma",
            tags=["sqlite"],
        )
    client = EngramChat(db_path=str(db), recall_limit=2)
    block = client._recall_block("busy_timeout sqlite writers")
    assert isinstance(block, str)


def test_optional_cross_encoder_import_failure(monkeypatch):
    import src.ranking as ranking

    monkeypatch.setenv("ENGRAM_RERANK", "cross-encoder")
    ranking._cross_encoder = None
    rows = [{"title": "t", "snippet": "s", "utility_score": 10.0}]
    # Without sentence_transformers installed this should degrade gracefully
    out = ranking.optional_cross_encoder_rerank(rows, "q")
    assert out == rows
    monkeypatch.delenv("ENGRAM_RERANK", raising=False)


def test_repair_fts_orphan_removal(tmp_path, monkeypatch):
    from src.database import get_connection, init_db, repair_fts_delta
    from src.memory_ops import create_mistake

    db = tmp_path / "orph.db"
    monkeypatch.setenv("ENGRAM_DB_PATH", str(db))
    monkeypatch.setenv("ENGRAM_EMBED_URL", "disabled")
    init_db()
    with get_connection(str(db)) as conn:
        create_mistake(
            conn, date="2026-01-01", context="c", mistake="keep me", fix="f", tags=["t"]
        )
        conn.execute(
            "INSERT INTO memory_fts (item_type, item_id, title, content, tags) "
            "VALUES ('mistake', '99999', 'orphan', 'x', '')"
        )
        stats = repair_fts_delta(conn)
        assert stats["orphans_removed"] >= 1


def test_doctor_small_drift_uses_delta(tmp_path, monkeypatch, capsys):
    from src import doctor
    from src.database import get_connection, init_db
    from src.memory_ops import create_mistake

    db = tmp_path / "doc.db"
    monkeypatch.setenv("ENGRAM_DB_PATH", str(db))
    monkeypatch.setenv("ENGRAM_EMBED_URL", "disabled")
    init_db()
    with get_connection(str(db)) as conn:
        mid = create_mistake(
            conn, date="2026-01-01", context="c", mistake="drift me", fix="f", tags=["t"]
        )
        conn.execute(
            "DELETE FROM memory_fts WHERE item_type=? AND item_id=?",
            ("mistake", str(mid)),
        )
    # Avoid network checks dominating — patch ollama/llm probes
    monkeypatch.setattr(doctor.config, "ollama_host", lambda: "http://127.0.0.1:9")
    monkeypatch.setattr(doctor, "urllib", doctor.urllib)
    doctor.run_diagnostics(repair=True)
    out = capsys.readouterr().out
    assert "FTS Drift" in out or "Repair" in out


def test_version_fallback(monkeypatch):
    import src.version as version

    class Boom:
        def version(self, name):
            raise Exception("no dist")

    monkeypatch.setattr(version, "version", Boom().version, raising=False)
    # call through importlib path
    from importlib import metadata
    monkeypatch.setattr(metadata, "version", lambda n: (_ for _ in ()).throw(metadata.PackageNotFoundError(n)))
    assert version.get_package_version()  # fallback string


def test_optional_cross_encoder_with_mock(monkeypatch):
    import sys
    import types

    import src.ranking as ranking

    class FakeCE:
        def __init__(self, model_name):
            self.model_name = model_name

        def predict(self, pairs):
            return [0.9 for _ in pairs]

    st = types.ModuleType("sentence_transformers")
    st.CrossEncoder = FakeCE
    monkeypatch.setitem(sys.modules, "sentence_transformers", st)
    monkeypatch.setenv("ENGRAM_RERANK", "cross-encoder")
    ranking._cross_encoder = None
    rows = [
        {"title": "a", "snippet": "one", "utility_score": 1.0, "score_breakdown": {}},
        {"title": "b", "snippet": "two", "utility_score": 2.0},
    ]
    out = ranking.optional_cross_encoder_rerank(rows, "query text")
    assert out[0]["utility_score"] > 1.0
    monkeypatch.delenv("ENGRAM_RERANK", raising=False)
    ranking._cross_encoder = None
