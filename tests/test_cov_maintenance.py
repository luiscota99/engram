"""Coverage tests for src/maintenance.py.

Seeds rows directly via the shared ``test_db`` fixture and exercises the
public maintenance functions, mocking the .llm / .merge boundaries so the
tests stay hermetic (no Ollama / network).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from src.maintenance import (
    _apply_auto_merge,
    _cluster_snippets,
    _cosine_similarity,
    _count_live_items,
    _enrich_gc_candidates,
    _gc_would_exceed_guard,
    _insert_merged_item,
    _UnionFind,
    archive_item,
    find_consolidation_candidates,
    find_gc_candidates,
    get_efficiency_report,
    get_reuse_rates,
    llm_audit_clusters,
    llm_gc_score_candidates,
    merge_projects,
    run_gc,
    run_health_check,
    run_llm_consolidation_audit,
    run_llm_gc,
    run_self_check,
    run_sleep,
)

OLD = (datetime.now() - timedelta(days=400)).isoformat()
RECENT = datetime.now().isoformat()


# ── seeding helpers ──────────────────────────────────────────────────


def _add_mistake(conn, mistake="m", *, usage=0, created=OLD, last_used=None):
    cur = conn.execute(
        "INSERT INTO mistakes (date, context, mistake, fix, usage_count, created_at, last_used_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("2026-01-01", "ctx", mistake, "the fix", usage, created, last_used),
    )
    return cur.lastrowid


def _add_skill(conn, name="s", *, usage=0, workflow="wf", trigger="trg", created=RECENT):
    cur = conn.execute(
        "INSERT INTO skills (name, domain, trigger_desc, workflow, usage_count, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (name, "eng", trigger, workflow, usage, created),
    )
    return cur.lastrowid


def _seed_fts_vec(conn, item_type, item_id, title, vec):
    cur = conn.execute(
        "INSERT INTO memory_fts (item_type, item_id, title, content, tags) VALUES (?, ?, ?, ?, ?)",
        (item_type, str(item_id), title, title, ""),
    )
    rid = cur.lastrowid
    conn.execute("INSERT INTO vec_memory(rowid, embedding) VALUES (?, ?)", (rid, json.dumps(vec)))
    return rid


def _vec(sig, val=1.0):
    v = [0.0] * 768
    v[sig] = val
    return v


# ── _cosine_similarity ───────────────────────────────────────────────


def test_cosine_similarity_length_mismatch():
    assert _cosine_similarity([1.0, 2.0], [1.0]) == 0.0


def test_cosine_similarity_zero_vector():
    assert _cosine_similarity([0.0, 0.0], [1.0, 1.0]) == 0.0


def test_cosine_similarity_identical_and_orthogonal():
    assert _cosine_similarity([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert _cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)


# ── _UnionFind ───────────────────────────────────────────────────────


def test_unionfind_transitive_merge():
    uf = _UnionFind()
    uf.union("a", "b")
    uf.union("b", "c")
    # a, b, c share a root; d is separate
    assert uf.find("a") == uf.find("c")
    assert uf.find("a") != uf.find("d")


# ── find_gc_candidates ───────────────────────────────────────────────


def test_find_gc_candidates_never_used_and_stale(test_db):
    conn = test_db["conn"]
    never = _add_mistake(conn, "never used old", usage=0, created=OLD)
    stale = _add_mistake(conn, "stale", usage=3, created=RECENT, last_used=OLD)
    _add_mistake(conn, "fresh unused", usage=0, created=RECENT)  # not a candidate
    conn.commit()

    cands = find_gc_candidates(days_unused=180, item_types=["mistake"], db_path=test_db["path"])
    ids = {c["item_id"] for c in cands}
    assert never in ids
    assert stale in ids
    row = next(c for c in cands if c["item_id"] == never)
    assert row["item_type"] == "mistake"
    assert row["usage_count"] == 0


def test_find_gc_candidates_skips_unknown_type(test_db):
    # An unknown item_type has no table → skipped, no crash.
    cands = find_gc_candidates(item_types=["bogus"], db_path=test_db["path"])
    assert cands == []


# ── _count_live_items ────────────────────────────────────────────────


def test_count_live_items(test_db):
    conn = test_db["conn"]
    _add_mistake(conn, "a")
    _add_mistake(conn, "b")
    conn.commit()
    assert _count_live_items(item_types=["mistake"], db_path=test_db["path"]) == 2


# ── _gc_would_exceed_guard ───────────────────────────────────────────


def test_guard_below_min_count_returns_none(test_db):
    cands = [{"item_type": "mistake", "item_id": i} for i in range(3)]
    assert _gc_would_exceed_guard(cands, ["mistake"], db_path=test_db["path"]) is None


def test_guard_blocks_when_fraction_exceeded(test_db):
    conn = test_db["conn"]
    ids = [_add_mistake(conn, f"m{i}") for i in range(10)]
    conn.commit()
    cands = [{"item_type": "mistake", "item_id": i} for i in ids]  # 10/10 = 100%
    reason = _gc_would_exceed_guard(cands, ["mistake"], db_path=test_db["path"])
    assert reason is not None
    assert "GC blocked" in reason
    assert "100%" in reason


# ── archive_item ─────────────────────────────────────────────────────


def test_archive_item_unknown_type_returns_false(test_db):
    assert archive_item(test_db["conn"], "bogus", 1) is False


def test_archive_item_missing_row_returns_false(test_db):
    assert archive_item(test_db["conn"], "mistake", 99999) is False


def test_archive_item_copies_then_deletes(test_db):
    conn = test_db["conn"]
    mid = _add_mistake(conn, "to archive")
    conn.commit()

    assert archive_item(conn, "mistake", mid, reason="unit") is True
    # Row removed from live table
    assert conn.execute("SELECT COUNT(*) c FROM mistakes WHERE id=?", (mid,)).fetchone()["c"] == 0
    # Copied into archived_memories with the reason + serialized data
    arc = conn.execute(
        "SELECT item_type, item_id, original_table, data, archive_reason FROM archived_memories "
        "WHERE item_type='mistake' AND item_id=?",
        (mid,),
    ).fetchone()
    assert arc["archive_reason"] == "unit"
    assert arc["original_table"] == "mistakes"
    assert json.loads(arc["data"])["mistake"] == "to archive"


# ── run_gc ───────────────────────────────────────────────────────────


def test_run_gc_dry_run_reports_without_mutating(test_db):
    conn = test_db["conn"]
    _add_mistake(conn, "old", usage=0, created=OLD)
    conn.commit()
    rep = run_gc(mode="dry-run", item_types=["mistake"], db_path=test_db["path"])
    assert rep["mode"] == "dry-run"
    assert rep["processed"] == 0
    assert rep["blocked"] is False
    assert len(rep["candidates"]) == 1
    # nothing deleted
    assert conn.execute("SELECT COUNT(*) c FROM mistakes").fetchone()["c"] == 1


def test_run_gc_archive_mode_processes(test_db):
    conn = test_db["conn"]
    _add_mistake(conn, "old", usage=0, created=OLD)
    conn.commit()
    rep = run_gc(mode="archive", item_types=["mistake"], db_path=test_db["path"])
    assert rep["processed"] == 1
    assert rep["blocked"] is False
    assert conn.execute("SELECT COUNT(*) c FROM mistakes").fetchone()["c"] == 0
    assert conn.execute("SELECT COUNT(*) c FROM archived_memories").fetchone()["c"] == 1


def test_run_gc_delete_mode_permanently_removes(test_db):
    conn = test_db["conn"]
    _add_mistake(conn, "old", usage=0, created=OLD)
    conn.commit()
    rep = run_gc(mode="delete", item_types=["mistake"], db_path=test_db["path"])
    assert rep["processed"] == 1
    assert conn.execute("SELECT COUNT(*) c FROM mistakes").fetchone()["c"] == 0
    # delete does NOT archive
    assert conn.execute("SELECT COUNT(*) c FROM archived_memories").fetchone()["c"] == 0


def test_run_gc_blocked_by_guard(test_db):
    conn = test_db["conn"]
    for i in range(10):
        _add_mistake(conn, f"old{i}", usage=0, created=OLD)
    conn.commit()
    rep = run_gc(mode="archive", item_types=["mistake"], db_path=test_db["path"])
    assert rep["blocked"] is True
    assert rep["processed"] == 0
    assert "GC blocked" in rep["reason"]
    # guard prevented any archival
    assert conn.execute("SELECT COUNT(*) c FROM mistakes").fetchone()["c"] == 10


# ── find_consolidation_candidates ────────────────────────────────────


def test_consolidation_sqlite_vec_unavailable(test_db):
    with patch("src.maintenance._SQLITE_VEC", False):
        clusters, reason = find_consolidation_candidates(db_path=test_db["path"])
    assert clusters == []
    assert reason == "sqlite_vec_unavailable"


def test_consolidation_finds_cluster(test_db):
    conn = test_db["conn"]
    a = _add_mistake(conn, "dup a", created=RECENT)
    b = _add_mistake(conn, "dup b", created=RECENT)
    c = _add_mistake(conn, "different", created=RECENT)
    _seed_fts_vec(conn, "mistake", a, "dup a", _vec(0))
    _seed_fts_vec(conn, "mistake", b, "dup b", _vec(0))  # same direction as a
    _seed_fts_vec(conn, "mistake", c, "different", _vec(500))  # orthogonal
    conn.commit()

    clusters, reason = find_consolidation_candidates(
        threshold=0.8, item_types=["mistake"], db_path=test_db["path"], force_rescan=True
    )
    assert reason is None
    assert len(clusters) == 1
    cl = clusters[0]
    assert cl["item_type"] == "mistake"
    assert cl["cluster_size"] == 2
    assert cl["avg_similarity"] == pytest.approx(1.0)
    assert {i["item_id"] for i in cl["items"]} == {str(a), str(b)}


def test_consolidation_unchanged_fingerprint_skips(test_db):
    conn = test_db["conn"]
    a = _add_mistake(conn, "dup a", created=RECENT)
    b = _add_mistake(conn, "dup b", created=RECENT)
    _seed_fts_vec(conn, "mistake", a, "dup a", _vec(0))
    _seed_fts_vec(conn, "mistake", b, "dup b", _vec(0))
    conn.commit()

    # first scan saves fingerprint
    find_consolidation_candidates(item_types=["mistake"], db_path=test_db["path"], force_rescan=True)
    # second scan with unchanged data → skipped
    clusters, reason = find_consolidation_candidates(
        item_types=["mistake"], db_path=test_db["path"]
    )
    assert clusters == []
    assert reason == "unchanged"


# ── _cluster_snippets ────────────────────────────────────────────────


def test_cluster_snippets_builds_from_items(test_db):
    conn = test_db["conn"]
    mid = _add_mistake(conn, "snippet mistake")
    conn.commit()
    cluster = {"item_type": "mistake", "items": [{"item_id": mid, "title": "T"}]}
    snips = _cluster_snippets(cluster, db_path=test_db["path"])
    assert len(snips) == 1
    assert snips[0]["id"] == mid
    assert "snippet mistake" in snips[0]["snippet"]


# ── _insert_merged_item ──────────────────────────────────────────────


def test_insert_merged_mistake(test_db):
    conn = test_db["conn"]
    mid = _insert_merged_item(conn, "mistake", {"mistake": "merged M", "fix": "f"}, ["t1"])
    assert mid is not None
    row = conn.execute("SELECT mistake FROM mistakes WHERE id=?", (mid,)).fetchone()
    assert row["mistake"] == "merged M"
    # FTS row created
    fts = conn.execute(
        "SELECT title FROM memory_fts WHERE item_type='mistake' AND item_id=?", (str(mid),)
    ).fetchone()
    assert fts["title"] == "merged M"


def test_insert_merged_pattern_and_skill(test_db):
    conn = test_db["conn"]
    pid = _insert_merged_item(conn, "pattern", {"name": "P", "symptoms": "s"}, [])
    sid = _insert_merged_item(conn, "skill", {"name": "S", "workflow": "w"}, [])
    assert conn.execute("SELECT name FROM patterns WHERE id=?", (pid,)).fetchone()["name"] == "P"
    assert conn.execute("SELECT name FROM skills WHERE id=?", (sid,)).fetchone()["name"] == "S"


def test_insert_merged_unknown_type_returns_none(test_db):
    assert _insert_merged_item(test_db["conn"], "bogus", {}, []) is None


# ── _apply_auto_merge ────────────────────────────────────────────────


def test_apply_auto_merge_requires_two_items(test_db):
    cluster = {"item_type": "mistake", "items": [{"item_id": 1}]}
    res = _apply_auto_merge(cluster, db_path=test_db["path"])
    assert res["applied"] is False
    assert "exactly 2" in res["reason"]


def test_apply_auto_merge_missing_item(test_db):
    cluster = {"item_type": "mistake", "items": [{"item_id": 111}, {"item_id": 222}]}
    res = _apply_auto_merge(cluster, db_path=test_db["path"])
    assert res["applied"] is False
    assert "not found" in res["reason"]


def test_apply_auto_merge_llm_merge_failed(test_db):
    conn = test_db["conn"]
    a = _add_mistake(conn, "a")
    b = _add_mistake(conn, "b")
    conn.commit()
    cluster = {"item_type": "mistake", "items": [{"item_id": a}, {"item_id": b}]}
    with patch("src.merge.merge_entries", return_value=None):
        res = _apply_auto_merge(cluster, db_path=test_db["path"])
    assert res["applied"] is False
    assert res["reason"] == "LLM merge failed"


def test_apply_auto_merge_success_archives_originals(test_db):
    conn = test_db["conn"]
    a = _add_mistake(conn, "orig a")
    b = _add_mistake(conn, "orig b")
    conn.commit()
    cluster = {"item_type": "mistake", "items": [{"item_id": a}, {"item_id": b}]}
    merged = {"mistake": "combined", "fix": "combined fix"}
    with patch("src.merge.merge_entries", return_value=merged):
        res = _apply_auto_merge(cluster, db_path=test_db["path"])
    assert res["applied"] is True
    assert res["archived_ids"] == [a, b]
    assert res["item_type"] == "mistake"
    # originals archived, merged row present
    live = {r["mistake"] for r in conn.execute("SELECT mistake FROM mistakes").fetchall()}
    assert "combined" in live
    assert "orig a" not in live and "orig b" not in live


# ── llm_audit_clusters ───────────────────────────────────────────────


def test_llm_audit_empty_clusters_returns_empty(test_db):
    assert llm_audit_clusters([], db_path=test_db["path"]) == []


def test_llm_audit_unavailable_returns_empty(test_db):
    with patch("src.llm.is_llm_available", return_value=False):
        assert llm_audit_clusters([{"item_type": "mistake", "items": []}], db_path=test_db["path"]) == []


def test_llm_audit_parses_decisions(test_db):
    conn = test_db["conn"]
    a = _add_mistake(conn, "a")
    b = _add_mistake(conn, "b")
    conn.commit()
    clusters = [
        {
            "item_type": "mistake",
            "avg_similarity": 0.9,
            "items": [{"item_id": a, "title": "a"}, {"item_id": b, "title": "b"}],
        }
    ]
    llm_json = json.dumps([{"cluster_index": 0, "decision": "auto_merge", "reason": "dup", "ids": [a, b]}])
    with patch("src.llm.is_llm_available", return_value=True), \
         patch("src.llm.call_chat_completion", return_value=llm_json), \
         patch("src.llm.resolve_llm_model", return_value="test-model"):
        decisions = llm_audit_clusters(clusters, db_path=test_db["path"])
    assert len(decisions) == 1
    d = decisions[0]
    assert d["decision"] == "auto_merge"
    assert d["item_type"] == "mistake"
    assert d["ids"] == [a, b]
    assert d["model"] == "test-model"


def test_llm_audit_normalizes_bad_decision(test_db):
    """A decision value outside the allowed set falls back to keep_both and
    ids default to the cluster's item ids."""
    conn = test_db["conn"]
    a = _add_mistake(conn, "a")
    conn.commit()
    clusters = [{"item_type": "mistake", "avg_similarity": 0.9, "items": [{"item_id": a, "title": "a"}]}]
    llm_json = json.dumps([{"cluster_index": 0, "decision": "nonsense"}])
    with patch("src.llm.is_llm_available", return_value=True), \
         patch("src.llm.call_chat_completion", return_value=llm_json), \
         patch("src.llm.resolve_llm_model", return_value="m"):
        decisions = llm_audit_clusters(clusters, db_path=test_db["path"])
    assert decisions[0]["decision"] == "keep_both"
    assert decisions[0]["ids"] == [a]


# ── run_llm_consolidation_audit ──────────────────────────────────────


def _seed_two_dup_mistakes(conn):
    a = _add_mistake(conn, "dup a", created=RECENT)
    b = _add_mistake(conn, "dup b", created=RECENT)
    _seed_fts_vec(conn, "mistake", a, "dup a", _vec(0))
    _seed_fts_vec(conn, "mistake", b, "dup b", _vec(0))
    conn.commit()
    return a, b


def test_consolidation_audit_no_clusters(test_db):
    with patch("src.llm.is_llm_available", return_value=True), \
         patch("src.llm.get_llm_status", return_value={"available": True}):
        rep = run_llm_consolidation_audit(db_path=test_db["path"], force_rescan=True)
    assert rep["clusters_found"] == 0
    assert rep["decisions"] == []
    assert rep["llm_status"] == {"available": True}


def test_consolidation_audit_llm_unavailable_fallback(test_db):
    conn = test_db["conn"]
    _seed_two_dup_mistakes(conn)
    with patch("src.llm.is_llm_available", return_value=False), \
         patch("src.llm.get_llm_status", return_value={"available": False}):
        rep = run_llm_consolidation_audit(db_path=test_db["path"], force_rescan=True)
    assert rep["clusters_found"] == 1
    assert "fallback" in rep
    assert rep["clusters"]


def test_consolidation_audit_dry_run_returns_decisions(test_db):
    conn = test_db["conn"]
    a, b = _seed_two_dup_mistakes(conn)
    llm_json = json.dumps([{"cluster_index": 0, "decision": "merge", "reason": "r", "ids": [a, b]}])
    with patch("src.llm.is_llm_available", return_value=True), \
         patch("src.llm.get_llm_status", return_value={"available": True}), \
         patch("src.llm.call_chat_completion", return_value=llm_json), \
         patch("src.llm.resolve_llm_model", return_value="m"):
        rep = run_llm_consolidation_audit(db_path=test_db["path"], dry_run=True, force_rescan=True)
    assert rep["dry_run"] is True
    assert len(rep["decisions"]) == 1
    assert rep["decisions"][0]["decision"] == "merge"
    assert "clusters" in rep


def test_consolidation_audit_applies_auto_merge(test_db):
    conn = test_db["conn"]
    a, b = _seed_two_dup_mistakes(conn)
    llm_json = json.dumps([{"cluster_index": 0, "decision": "auto_merge", "reason": "r", "ids": [a, b]}])
    merged = {"mistake": "auto combined", "fix": "f"}
    with patch("src.llm.is_llm_available", return_value=True), \
         patch("src.llm.get_llm_status", return_value={"available": True}), \
         patch("src.llm.call_chat_completion", return_value=llm_json), \
         patch("src.llm.resolve_llm_model", return_value="m"), \
         patch("src.merge.merge_entries", return_value=merged):
        rep = run_llm_consolidation_audit(db_path=test_db["path"], dry_run=False, force_rescan=True)
    assert len(rep["applied"]) == 1
    assert rep["applied"][0]["applied"] is True
    live = {r["mistake"] for r in conn.execute("SELECT mistake FROM mistakes").fetchall()}
    assert "auto combined" in live


def test_consolidation_audit_unchanged_short_circuits(test_db):
    conn = test_db["conn"]
    _seed_two_dup_mistakes(conn)
    with patch("src.llm.is_llm_available", return_value=True), \
         patch("src.llm.get_llm_status", return_value={"available": True}):
        # prime fingerprint
        run_llm_consolidation_audit(db_path=test_db["path"], force_rescan=True)
        rep = run_llm_consolidation_audit(db_path=test_db["path"])
    assert rep["skip_reason"] == "unchanged"
    assert rep["clusters_found"] == 0


# ── _enrich_gc_candidates ────────────────────────────────────────────


def test_enrich_gc_candidates(test_db):
    conn = test_db["conn"]
    mid = _add_mistake(conn, "enrich me")
    _seed_fts_vec(conn, "mistake", mid, "Enrich Title", _vec(1))
    # give the fts row real content
    conn.execute(
        "UPDATE memory_fts SET content=? WHERE item_type='mistake' AND item_id=?",
        ("some searchable body", str(mid)),
    )
    conn.commit()
    cands = [{"item_type": "mistake", "item_id": mid, "usage_count": 0}]
    enriched = _enrich_gc_candidates(cands, db_path=test_db["path"])
    assert enriched[0]["title"] == "Enrich Title"
    assert enriched[0]["snippet"] == "some searchable body"


# ── llm_gc_score_candidates ──────────────────────────────────────────


def test_llm_gc_score_empty_or_unavailable(test_db):
    assert llm_gc_score_candidates([], db_path=test_db["path"]) == []
    with patch("src.llm.is_llm_available", return_value=False):
        assert llm_gc_score_candidates([{"item_type": "mistake", "item_id": 1}], db_path=test_db["path"]) == []


def test_llm_gc_score_parses(test_db):
    conn = test_db["conn"]
    mid = _add_mistake(conn, "scoreme")
    _seed_fts_vec(conn, "mistake", mid, "T", _vec(2))
    conn.commit()
    cands = [{"item_type": "mistake", "item_id": mid, "usage_count": 0}]
    llm_json = json.dumps([
        {"item_type": "mistake", "item_id": mid, "decision": "discard", "reason": "obsolete"},
        {"item_type": "mistake", "item_id": mid, "decision": "weird"},  # normalized to keep
    ])
    with patch("src.llm.is_llm_available", return_value=True), \
         patch("src.llm.call_chat_completion", return_value=llm_json):
        scored = llm_gc_score_candidates(cands, db_path=test_db["path"])
    assert scored[0]["decision"] == "discard"
    assert scored[0]["reason"] == "obsolete"
    assert scored[1]["decision"] == "keep"


# ── run_llm_gc ───────────────────────────────────────────────────────


def test_run_llm_gc_no_candidates(test_db):
    with patch("src.llm.is_llm_available", return_value=True), \
         patch("src.llm.get_llm_status", return_value={"available": True}):
        rep = run_llm_gc(item_types=["mistake"], db_path=test_db["path"])
    assert rep["candidates"] == []
    assert rep["processed"] == 0


def test_run_llm_gc_unavailable_dry_run_fallback(test_db):
    conn = test_db["conn"]
    _add_mistake(conn, "old", usage=0, created=OLD)
    conn.commit()
    with patch("src.llm.is_llm_available", return_value=False), \
         patch("src.llm.get_llm_status", return_value={"available": False}):
        rep = run_llm_gc(dry_run=True, item_types=["mistake"], db_path=test_db["path"])
    # LLM unavailable → time-based fallback candidates, nothing processed in dry run
    assert len(rep["candidates"]) == 1
    assert rep["fallback"].startswith("LLM unavailable")
    assert rep["scored"] == []
    assert rep["processed"] == 0


def test_run_llm_gc_available_archives(test_db):
    conn = test_db["conn"]
    mid = _add_mistake(conn, "old", usage=0, created=OLD)
    _seed_fts_vec(conn, "mistake", mid, "T", _vec(3))
    conn.commit()
    llm_json = json.dumps([{"item_type": "mistake", "item_id": mid, "decision": "discard", "reason": "x"}])
    with patch("src.llm.is_llm_available", return_value=True), \
         patch("src.llm.get_llm_status", return_value={"available": True}), \
         patch("src.llm.call_chat_completion", return_value=llm_json):
        rep = run_llm_gc(dry_run=False, item_types=["mistake"], db_path=test_db["path"])
    assert rep["processed"] == 1
    assert len(rep["to_discard"]) == 1
    assert conn.execute("SELECT COUNT(*) c FROM mistakes WHERE id=?", (mid,)).fetchone()["c"] == 0


def test_run_llm_gc_blocked_by_guard(test_db):
    conn = test_db["conn"]
    ids = [_add_mistake(conn, f"old{i}", usage=0, created=OLD) for i in range(10)]
    conn.commit()
    scored = [{"item_type": "mistake", "item_id": i, "decision": "discard", "reason": "x"} for i in ids]
    with patch("src.llm.is_llm_available", return_value=True), \
         patch("src.llm.get_llm_status", return_value={"available": True}), \
         patch("src.maintenance.llm_gc_score_candidates", return_value=scored):
        rep = run_llm_gc(dry_run=False, item_types=["mistake"], db_path=test_db["path"])
    assert rep["blocked"] is True
    assert "GC blocked" in rep["reason"]
    assert conn.execute("SELECT COUNT(*) c FROM mistakes").fetchone()["c"] == 10


# ── merge_projects ───────────────────────────────────────────────────


def _add_project(conn, name, path):
    return conn.execute(
        "INSERT INTO projects (name, path) VALUES (?, ?)", (name, path)
    ).lastrowid


def test_merge_projects_same_id_raises(test_db):
    with pytest.raises(ValueError, match="must differ"):
        merge_projects(1, 1, db_path=test_db["path"])


def test_merge_projects_missing_project_raises(test_db):
    conn = test_db["conn"]
    p1 = _add_project(conn, "a", "/a")
    conn.commit()
    with pytest.raises(ValueError, match="must exist"):
        merge_projects(p1, 99999, db_path=test_db["path"])


def test_merge_projects_dry_run_counts(test_db):
    conn = test_db["conn"]
    src = _add_project(conn, "src", "/src")
    dst = _add_project(conn, "dst", "/dst")
    # overlapping file_path + a unique one
    conn.execute("INSERT INTO codebase_knowledge (project_id, file_path, file_hash, summary) VALUES (?, ?, ?, ?)", (src, "shared.py", "h", "s"))
    conn.execute("INSERT INTO codebase_knowledge (project_id, file_path, file_hash, summary) VALUES (?, ?, ?, ?)", (src, "only_src.py", "h", "s"))
    conn.execute("INSERT INTO codebase_knowledge (project_id, file_path, file_hash, summary) VALUES (?, ?, ?, ?)", (dst, "shared.py", "h", "s"))
    conn.commit()

    summary = merge_projects(src, dst, dry_run=True, db_path=test_db["path"])
    assert summary["dry_run"] is True
    assert summary["from_name"] == "src"
    assert summary["to_name"] == "dst"
    assert summary["codebase_overlap_removed"] == 1
    assert summary["codebase_reassigned"] == 1
    assert summary["source_project_deleted"] is False
    # dry run mutates nothing
    assert conn.execute("SELECT COUNT(*) c FROM projects").fetchone()["c"] == 2


def test_merge_projects_apply_reassigns_and_deletes_source(test_db):
    conn = test_db["conn"]
    src = _add_project(conn, "src", "/src")
    dst = _add_project(conn, "dst", "/dst")
    conn.execute("INSERT INTO codebase_knowledge (project_id, file_path, file_hash, summary) VALUES (?, ?, ?, ?)", (src, "shared.py", "h", "s"))
    conn.execute("INSERT INTO codebase_knowledge (project_id, file_path, file_hash, summary) VALUES (?, ?, ?, ?)", (src, "only_src.py", "h", "s"))
    conn.execute("INSERT INTO codebase_knowledge (project_id, file_path, file_hash, summary) VALUES (?, ?, ?, ?)", (dst, "shared.py", "h", "s"))
    conn.execute(
        "INSERT INTO file_relationships (project_id, source_file, target_file, relationship_type) "
        "VALUES (?, ?, ?, ?)",
        (src, "a.py", "b.py", "imports"),
    )
    conn.execute("INSERT INTO item_projects (item_type, item_id, project_id) VALUES (?, ?, ?)", ("mistake", 1, src))
    conn.commit()

    summary = merge_projects(src, dst, dry_run=False, db_path=test_db["path"])
    assert summary["source_project_deleted"] is True
    # source project gone
    assert conn.execute("SELECT COUNT(*) c FROM projects WHERE id=?", (src,)).fetchone()["c"] == 0
    # overlapping dropped, unique reassigned to dst
    paths = {
        r["file_path"]
        for r in conn.execute("SELECT file_path FROM codebase_knowledge WHERE project_id=?", (dst,)).fetchall()
    }
    assert paths == {"shared.py", "only_src.py"}
    # relationship + item_project moved to dst
    assert conn.execute(
        "SELECT project_id FROM file_relationships WHERE source_file='a.py'"
    ).fetchone()["project_id"] == dst
    assert conn.execute(
        "SELECT project_id FROM item_projects WHERE item_id=1"
    ).fetchone()["project_id"] == dst


# ── get_reuse_rates ──────────────────────────────────────────────────


def test_get_reuse_rates(test_db):
    conn = test_db["conn"]
    old = (datetime.now() - timedelta(days=60)).isoformat()
    _add_mistake(conn, "reused old", usage=4, created=old)
    _add_mistake(conn, "unused old", usage=0, created=old)
    _add_mistake(conn, "recent", usage=0, created=RECENT)  # not eligible (< 30d)
    conn.commit()
    rates = get_reuse_rates(db_path=test_db["path"])
    m = rates["mistake"]
    assert m["eligible"] == 2
    assert m["reused"] == 1
    assert m["rate"] == 0.5
    # a gc-ineligible type must not appear
    assert "role" not in rates


# ── run_health_check ─────────────────────────────────────────────────


def test_run_health_check_recommendations(test_db):
    conn = test_db["conn"]
    # gc candidate (old, unused)
    _add_mistake(conn, "old unused", usage=0, created=OLD)
    # orphaned tag
    conn.execute("INSERT INTO tags (name) VALUES ('orphan')")
    # embedding_status: stale + pending
    conn.execute(
        "INSERT INTO embedding_status (fts_rowid, item_type, item_id, embedding_model, status) "
        "VALUES (1, 'mistake', '1', 'm', 'stale')"
    )
    conn.execute(
        "INSERT INTO embedding_status (fts_rowid, item_type, item_id, embedding_model, status) "
        "VALUES (2, 'mistake', '2', 'm', 'pending')"
    )
    # FTS row without vec → vec_drift > 0
    conn.execute(
        "INSERT INTO memory_fts (item_type, item_id, title, content, tags) VALUES ('mistake','1','t','c','')"
    )
    conn.commit()

    report = run_health_check(db_path=test_db["path"])
    assert "items" in report
    assert report["orphaned_tags"] == 1
    assert report["gc_candidates"] >= 1
    assert report["vec_drift"] >= 1
    assert report["embeddings"]["stale"] == 1
    assert report["embeddings"]["pending"] == 1
    recs = " ".join(report["recommendations"])
    assert "stale embeddings" in recs
    assert "no embeddings" in recs
    assert "engram gc --archive" in recs
    assert "orphaned tags" in recs
    assert "missing vector embeddings" in recs


# ── run_sleep ────────────────────────────────────────────────────────


def test_run_sleep_dry_run(test_db):
    conn = test_db["conn"]
    a, b = _seed_two_dup_mistakes(conn)
    rep = run_sleep(dry_run=True, db_path=test_db["path"])
    assert rep["dry_run"] is True
    assert rep["clusters_found"] == 1
    assert rep["items_invalidated"] == 0
    assert rep["items_archived"] == 0
    assert "gc_candidates" in rep


def test_run_sleep_applies_invalidation_and_archive(test_db):
    conn = test_db["conn"]
    # two duplicate, recent items → one cluster to invalidate
    a, b = _seed_two_dup_mistakes(conn)
    # a separate old unused item → archived by gc
    old = _add_mistake(conn, "stale old", usage=0, created=OLD)
    _seed_fts_vec(conn, "mistake", old, "stale old", _vec(600))
    conn.commit()

    with patch("src.temporal.invalidate_memory") as inv:
        rep = run_sleep(threshold=0.8, days_unused=180, dry_run=False, db_path=test_db["path"])
    assert rep["clusters_found"] == 1
    assert rep["items_invalidated"] == 1  # keeper + 1 superseded
    inv.assert_called_once()
    assert rep["items_archived"] >= 1


# ── get_efficiency_report ────────────────────────────────────────────


def test_get_efficiency_report(test_db):
    conn = test_db["conn"]
    sid = _add_skill(conn, "reflex skill", workflow="x" * 400, trigger="y" * 40)
    rid = conn.execute(
        "INSERT INTO reflexes (skill_id, name, description, script, interpreter, run_count, approved_at) "
        "VALUES (?, ?, ?, ?, ?, ?, datetime('now'))",
        (sid, "rfx", "desc", "echo hi", "bash", 3),
    ).lastrowid
    for _ in range(3):
        conn.execute(
            "INSERT INTO reflex_runs (reflex_id, started_at, duration_ms, status) VALUES (?, datetime('now'), ?, 'ok')", (rid, 10)
        )
    conn.execute(
        "INSERT INTO mistakes (date, context, mistake, fix) VALUES ('2026-01-01','c','Auto-demoted rfx','f')"
    )
    conn.commit()

    report = get_efficiency_report(db_path=test_db["path"])
    assert report["reflexes_approved"] == 1
    assert report["reflexes_total"] == 1
    assert report["reflex_runs"] == 3
    assert report["auto_demotions"] == 1
    # floor = ((400 + 40)//4 - 50) * 3 = (110 - 50) * 3 = 180
    assert report["tokens_avoided_floor"] == 180
    assert "mistake" in report["reuse"]
    assert isinstance(report["reflex_success"], dict)


# ── run_self_check ───────────────────────────────────────────────────


def test_run_self_check_files_findings(test_db):
    conn = test_db["conn"]
    # promotion + unvalidated candidate: reused skill, no reflex, no test
    sid = _add_skill(conn, "proven skill", usage=6)
    # flaky reflex: 5 runs, 2 ok (40%)
    fsid = _add_skill(conn, "flaky skill", usage=1)
    rid = conn.execute(
        "INSERT INTO reflexes (skill_id, name, description, script, interpreter, approved_at) "
        "VALUES (?, 'flaky', 'd', 's', 'bash', datetime('now'))",
        (fsid,),
    ).lastrowid
    for status in ["ok", "ok", "error", "error", "error"]:
        conn.execute(
            "INSERT INTO reflex_runs (reflex_id, started_at, duration_ms, status) VALUES (?, datetime('now'), 5, ?)", (rid, status)
        )
    # placeholder mistake
    conn.execute(
        "INSERT INTO mistakes (date, context, mistake, fix) VALUES ('2026-01-01','c','m','(fill in the cause)')"
    )
    # >5 pending embeddings
    for i in range(6):
        conn.execute(
            "INSERT INTO embedding_status (fts_rowid, item_type, item_id, embedding_model, status) "
            "VALUES (?, 'mistake', ?, 'm', 'pending')",
            (100 + i, str(i)),
        )
    # consolidation cluster
    a, b = _seed_two_dup_mistakes(conn)
    conn.commit()

    result = run_self_check(db_path=test_db["path"])
    keys = set(result["filed"])
    assert result["count"] == len(result["filed"])
    assert f"promote:skill:{sid}" in keys
    assert f"unvalidated:skill:{sid}" in keys
    assert f"reflex-flaky:{rid}" in keys
    assert "hygiene:placeholders" in keys
    assert "hygiene:pending-embeddings" in keys
    assert any(k.startswith("consolidate:mistake:") for k in keys)
    # inbox rows actually written
    n = conn.execute("SELECT COUNT(*) c FROM inbox WHERE source='self_check'").fetchone()["c"]
    assert n == result["count"]
