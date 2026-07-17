"""Tests for audit-log label mining: filtering, dedup, label format, append."""

from __future__ import annotations

import io
import json
import sys
from types import SimpleNamespace

import pytest

from src import label_mining as lm


def _audit(tmp_path, records):
    p = tmp_path / "audit.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
    return str(p)


def test_mine_filters_sources_length_and_injected(tmp_path):
    path = _audit(tmp_path, [
        {"source": "cli", "query": "ollama cold start embedding timeout"},
        {"source": "guard", "query": "git commit -m something long enough"},  # tool payload
        {"source": "hook", "query": "hi"},                                     # too short
        {"source": "mcp", "query": "<system-reminder> injected content here"},
        {"source": "cli", "query": "x" * 400},                                 # too long
        {"source": "hook", "query": "how do we deploy the staging container"},
    ])
    got = lm.mine_candidates(path, [], limit=10)
    queries = [c["query"] for c in got]
    assert "ollama cold start embedding timeout" in queries
    assert "how do we deploy the staging container" in queries
    assert len(queries) == 2


def test_mine_dedups_against_labels_and_batch(tmp_path):
    path = _audit(tmp_path, [
        {"source": "cli", "query": "Ollama Cold Start embedding timeout"},   # case dup of label
        {"source": "cli", "query": "fresh unlabeled query about reflexes"},
        {"source": "cli", "query": "fresh   unlabeled query about reflexes"},  # ws dup in batch
    ])
    existing = [{"query": "ollama cold start embedding timeout"}]
    got = lm.mine_candidates(path, existing, limit=10)
    assert len(got) == 1
    # newest-first iteration keeps the last-logged variant's raw text;
    # normalization is for dedup only
    assert lm._normalize(got[0]["query"]) == "fresh unlabeled query about reflexes"


def test_mine_newest_first_and_limit(tmp_path):
    path = _audit(tmp_path, [
        {"source": "cli", "query": f"numbered real query about topic {i}"} for i in range(10)
    ])
    got = lm.mine_candidates(path, [], limit=3)
    assert len(got) == 3
    assert got[0]["query"].endswith("topic 9")  # newest first


def test_build_label_id_grading_and_abstention():
    existing = [{"id": "mined_how_do_we_deploy"}]
    item = {"item_type": "mistake", "item_id": "23", "title": "Piped pytest -q output"}
    lbl = lm.build_label("how do we deploy", existing=existing, item=item)
    assert lbl["expected_type"] == "mistake"
    assert lbl["expected_item_id"] == 23  # int, matches normalize_item_id grading
    assert lbl["expected_title_contains"].startswith("Piped pytest")
    assert lbl["id"] != "mined_how_do_we_deploy"  # slug dedup

    ab = lm.build_label("anything about quantum llamas", existing=[], abstention=True)
    assert ab["expect_abstention"] is True
    assert "expected_type" not in ab

    with pytest.raises(ValueError):
        lm.build_label("q", existing=[], item=None)


def test_append_labels_atomic_roundtrip(tmp_path):
    path = str(tmp_path / "real_queries.json")
    total = lm.append_labels(path, [{"id": "a", "query": "q1"}])
    assert total == 1
    total = lm.append_labels(path, [{"id": "b", "query": "q2"}])
    assert total == 2
    assert [q["id"] for q in lm.load_label_set(path)] == ["a", "b"]


def test_cli_non_tty_lists_without_labeling(tmp_path, monkeypatch):
    from src.cli.commands.tools import cmd_bench_label

    audit = _audit(tmp_path, [{"source": "cli", "query": "list me without labeling please"}])
    queries = str(tmp_path / "rq.json")
    monkeypatch.setattr("sys.stdin", io.StringIO(""))  # not a tty
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    cmd_bench_label(SimpleNamespace(audit=audit, queries=queries, count=5))
    out = buf.getvalue()
    assert "list me without labeling" in out
    assert lm.load_label_set(queries) == []  # nothing written without a choice
