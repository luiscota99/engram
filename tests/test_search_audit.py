"""Tests for optional ENGRAM_AUDIT_LOG append in hybrid search."""
from __future__ import annotations

import json

from src.database import get_connection, index_in_fts, link_tags


def _seed_skill(db_path: str) -> None:
    with get_connection(db_path) as conn:
        cursor = conn.execute(
            "INSERT INTO skills (name, domain, trigger_desc, workflow) VALUES (?, ?, ?, ?)",
            ("Audit Skill", "engineering", "trigger", "workflow body"),
        )
        sid = cursor.lastrowid
        link_tags(conn, "skill", sid, ["t"])
        index_in_fts(conn, "skill", sid, "Audit Skill", "trigger | workflow body", ["t"])


def test_audit_log_appends_when_env_set(test_db, tmp_path, monkeypatch):
    _seed_skill(test_db["path"])
    log = tmp_path / "audit.jsonl"
    monkeypatch.setenv("ENGRAM_AUDIT_LOG", str(log))

    from src.search import search

    results = search("Audit", limit=5, db_path=test_db["path"], audit_source="test")

    assert len(results) >= 1
    assert log.exists()
    line = json.loads(log.read_text().strip())
    assert line["source"] == "test"
    assert line["query"] == "Audit"
    assert line["top_k"]
    assert line["top_k"][0]["item_type"] == "skill"


def test_skip_audit_skips_file(test_db, tmp_path, monkeypatch):
    _seed_skill(test_db["path"])
    log = tmp_path / "audit.jsonl"
    monkeypatch.setenv("ENGRAM_AUDIT_LOG", str(log))

    from src.search import search

    search("Audit", limit=5, db_path=test_db["path"], skip_audit=True)
    assert not log.exists() or log.read_text() == ""


def test_append_search_audit_records_latency(tmp_path, monkeypatch):
    log = tmp_path / "audit.jsonl"
    monkeypatch.setenv("ENGRAM_AUDIT_LOG", str(log))

    from src.search_audit import append_search_audit

    append_search_audit(
        query="q",
        results=[],
        semantic_status="ok",
        source="test",
        item_type=None,
        tags=None,
        limit=5,
        project_path=None,
        embed_ms=1189.0,
        vec_search_ms=12.5,
    )
    line = json.loads(log.read_text().strip())
    assert line["embed_ms"] == 1189.0
    assert line["vec_search_ms"] == 12.5


def test_summarize_aggregates_latency_percentiles(tmp_path, monkeypatch):
    log = tmp_path / "audit.jsonl"
    monkeypatch.setenv("ENGRAM_AUDIT_LOG", str(log))

    # Ten samples: embedding dominant (~1s), KNN cheap (~10ms) — the empirical shape.
    lines = []
    for i in range(10):
        lines.append(json.dumps({
            "ts": f"2026-07-20T00:00:{i:02d}+00:00",
            "source": "hook",
            "query": f"q{i}",
            "result_count": 3,
            "embed_ms": 1000.0 + i * 10,      # 1000..1090
            "vec_search_ms": 10.0 + i,        # 10..19
        }))
    log.write_text("\n".join(lines) + "\n")

    from src.search_audit import summarize_audit_log

    out = summarize_audit_log(str(log))
    lat = out["latency"]
    assert lat["samples"] == 10
    # embedding p50 well above KNN p50 — the ratio the ROI verdict keys on
    assert lat["embed_ms"]["p50"] >= 1000.0
    assert lat["vec_search_ms"]["p50"] < 30.0
    assert lat["embed_ms"]["max"] == 1090.0
    assert lat["vec_search_ms"]["max"] == 19.0


def test_summarize_latency_absent_when_no_samples(tmp_path, monkeypatch):
    log = tmp_path / "audit.jsonl"
    monkeypatch.setenv("ENGRAM_AUDIT_LOG", str(log))
    log.write_text(json.dumps({
        "ts": "2026-07-20T00:00:00+00:00", "source": "hook", "query": "q", "result_count": 1,
    }) + "\n")

    from src.search_audit import summarize_audit_log

    out = summarize_audit_log(str(log))
    assert out["latency"]["samples"] == 0
    assert out["latency"]["embed_ms"]["p50"] is None
