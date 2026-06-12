"""Tests for first-class LLM integration (routing, audit, GC, status)."""

from __future__ import annotations

import json
import os
from unittest.mock import patch

from src.llm import (
    get_llm_status,
    is_llm_available,
    parse_json_from_llm,
    resolve_llm_model,
)
from src.maintenance import (
    llm_audit_clusters,
    llm_gc_score_candidates,
    run_llm_consolidation_audit,
    run_llm_gc,
)
from src.mcp.handlers import handle_memory_llm_status


def test_resolve_llm_model_default():
    with patch.dict(os.environ, {}, clear=True):
        assert resolve_llm_model() == "llama3.2"


def test_resolve_llm_model_task_routing():
    env = {
        "ENGRAM_LLM_MODEL": "base-model",
        "ENGRAM_LLM_EXTRACT_MODEL": "fast-extract",
        "ENGRAM_LLM_AUDIT_MODEL": "smart-audit",
    }
    with patch.dict(os.environ, env, clear=True):
        assert resolve_llm_model(task="extract") == "fast-extract"
        assert resolve_llm_model(task="audit") == "smart-audit"
        assert resolve_llm_model(task="gc") == "smart-audit"
        assert resolve_llm_model() == "base-model"
        assert resolve_llm_model("explicit") == "explicit"


def test_is_llm_available_ollama_backend():
    with patch.dict(os.environ, {"ENGRAM_LLM_BASE_URL": "http://localhost:11434/v1"}, clear=True):
        with patch("urllib.request.urlopen") as mock_open:
            mock_open.return_value.__enter__.return_value.status = 200
            assert is_llm_available() is True
            called_url = mock_open.call_args[0][0].full_url
            assert called_url.endswith("/api/tags")


def test_is_llm_available_cloud_backend():
    env = {
        "ENGRAM_LLM_BASE_URL": "https://api.groq.com/openai/v1",
        "ENGRAM_LLM_API_KEY": "test-key",
    }
    with patch.dict(os.environ, env, clear=True):
        with patch("urllib.request.urlopen") as mock_open:
            mock_open.return_value.__enter__.return_value.status = 200
            assert is_llm_available() is True
            req = mock_open.call_args[0][0]
            assert req.get_header("Authorization") == "Bearer test-key"


def test_get_llm_status_structure():
    with patch("src.llm.is_llm_available", return_value=False):
        status = get_llm_status()
    assert "base_url" in status
    assert "model" in status
    assert "audit_model" in status
    assert "extract_model" in status
    assert status["available"] is False
    assert status["tasks_enabled"] == []


def test_get_llm_status_tasks_when_available():
    with patch("src.llm.is_llm_available", return_value=True):
        status = get_llm_status()
    assert status["available"] is True
    assert "consolidation_audit" in status["tasks_enabled"]
    assert "gc_scoring" in status["tasks_enabled"]


def test_llm_audit_clusters_parses_decisions():
    clusters = [
        {
            "item_type": "pattern",
            "items": [{"item_id": 1, "title": "A"}, {"item_id": 2, "title": "B"}],
            "avg_similarity": 0.9,
        }
    ]
    llm_response = json.dumps([
        {
            "cluster_index": 0,
            "decision": "auto_merge",
            "reason": "Same SQLite WAL fix",
            "ids": [1, 2],
        }
    ])
    with patch("src.llm.is_llm_available", return_value=True):
        with patch("src.llm.call_chat_completion", return_value=llm_response):
            with patch("src.maintenance._cluster_snippets", return_value=[
                {"id": 1, "title": "A", "snippet": "wal"},
                {"id": 2, "title": "B", "snippet": "wal"},
            ]):
                decisions = llm_audit_clusters(clusters, db_path="/tmp/x.db")

    assert len(decisions) == 1
    assert decisions[0]["decision"] == "auto_merge"
    assert decisions[0]["ids"] == [1, 2]


def test_llm_audit_clusters_unavailable_returns_empty():
    clusters = [{"item_type": "skill", "items": []}]
    with patch("src.llm.is_llm_available", return_value=False):
        assert llm_audit_clusters(clusters) == []


def test_llm_gc_score_candidates():
    candidates = [
        {"item_type": "mistake", "item_id": 5, "usage_count": 0, "created_at": "2020-01-01"},
    ]
    llm_response = json.dumps([
        {"item_type": "mistake", "item_id": 5, "decision": "discard", "reason": "obsolete"},
    ])
    with patch("src.llm.is_llm_available", return_value=True):
        with patch("src.llm.call_chat_completion", return_value=llm_response):
            with patch(
                "src.maintenance._enrich_gc_candidates",
                return_value=[{**candidates[0], "title": "old", "snippet": "ctx"}],
            ):
                scored = llm_gc_score_candidates(candidates, db_path="/tmp/x.db")

    assert len(scored) == 1
    assert scored[0]["decision"] == "discard"


def test_run_llm_consolidation_audit_fallback_without_llm(test_db):
    with patch("src.maintenance.find_consolidation_candidates", return_value=([], None)):
        report = run_llm_consolidation_audit(db_path=test_db["path"])
    assert report["clusters_found"] == 0
    assert "llm_status" in report


def test_run_llm_gc_dry_run_without_llm(test_db):
    conn = test_db["conn"]
    conn.execute(
        "INSERT INTO mistakes (date, context, mistake, fix, usage_count, created_at) "
        "VALUES ('2020-01-01', 'ctx', 'old bug', 'fix', 0, '2020-01-01')"
    )
    conn.commit()

    with patch("src.llm.is_llm_available", return_value=False):
        report = run_llm_gc(dry_run=True, days_unused=180, db_path=test_db["path"])

    assert len(report["candidates"]) >= 1
    assert report.get("fallback")
    assert report["processed"] == 0


def test_run_llm_gc_filters_discards(test_db):
    conn = test_db["conn"]
    conn.execute(
        "INSERT INTO mistakes (date, context, mistake, fix, usage_count, created_at) "
        "VALUES ('2020-01-01', 'ctx', 'old bug', 'fix', 0, '2020-01-01')"
    )
    conn.commit()

    scored = [{"item_type": "mistake", "item_id": 1, "decision": "discard", "reason": "stale"}]
    with patch("src.llm.is_llm_available", return_value=True):
        with patch("src.maintenance.llm_gc_score_candidates", return_value=scored):
            report = run_llm_gc(dry_run=True, days_unused=180, db_path=test_db["path"])

    assert len(report["to_discard"]) == 1
    assert report["to_discard"][0]["item_id"] == 1


def test_handle_memory_llm_status():
    with patch("src.llm.get_llm_status") as mock_status:
        mock_status.return_value = {
            "base_url": "http://localhost:11434/v1",
            "model": "llama3.2",
            "audit_model": "llama3.2",
            "extract_model": "llama3.2",
            "available": True,
            "tasks_enabled": ["merge"],
            "api_key_set": False,
        }
        out = handle_memory_llm_status({})
    parsed = json.loads(out)
    assert parsed["available"] is True
    assert "audit_model" in parsed


def test_parse_json_from_llm_fenced():
    raw = '```json\n[{"id": 1}]\n```'
    assert parse_json_from_llm(raw) == [{"id": 1}]
