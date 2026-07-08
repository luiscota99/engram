"""Tests for memory enhancements (pins, dedup, security, maintenance)."""

from __future__ import annotations

from unittest.mock import patch

from src.database import (
    SCHEMA_VERSION,
    check_duplicate_before_add,
    get_pinned_items,
    index_in_fts,
    pin_item,
    unpin_item,
)
from src.maintenance import find_consolidation_candidates, run_gc, run_sleep
from src.prompt_security import wrap_untrusted_text
from src.ranking import calculate_utility_score
from src.search import search


def test_schema_version_is_current():
    assert SCHEMA_VERSION == 16


def test_pin_and_search_prepend(test_db):
    conn = test_db["conn"]
    conn.execute("INSERT INTO memory_fts(memory_fts) VALUES('rebuild')")
    conn.execute(
        """INSERT INTO skills (name, domain, trigger_desc, workflow)
           VALUES ('Pinned skill', 'engineering', 'always', 'Always relevant workflow.')"""
    )
    conn.execute(
        """INSERT INTO skills (name, domain, trigger_desc, workflow)
           VALUES ('Other skill', 'engineering', 'other', 'Lexical match term xyzzy.')"""
    )
    index_in_fts(conn, "skill", 1, "Pinned skill", "Always relevant workflow.", ["core"])
    index_in_fts(conn, "skill", 2, "Other skill", "Lexical match term xyzzy.", ["other"])
    conn.commit()

    assert pin_item("skill", 1, db_path=test_db["path"])
    pinned = get_pinned_items(db_path=test_db["path"])
    assert len(pinned) == 1
    assert pinned[0]["title"] == "Pinned skill"

    results = search("xyzzy", db_path=test_db["path"], limit=5)
    assert len(results) >= 2
    assert results[0]["pinned"] is True
    assert results[0]["item_id"] == "1"

    assert unpin_item("skill", 1, db_path=test_db["path"])


def test_write_time_dedup_exact_name(test_db):
    conn = test_db["conn"]
    conn.execute(
        "INSERT INTO patterns (name, symptoms, root_cause, standard_fix) VALUES (?, ?, ?, ?)",
        ("SQLite WAL", "locks", "journal mode", "enable WAL"),
    )
    conn.commit()

    dedup = check_duplicate_before_add(
        "symptoms | cause | fix",
        "pattern",
        name="SQLite WAL",
        db_path=test_db["path"],
    )
    assert dedup["exact_match"] is True
    assert dedup["duplicates"]


def test_untrusted_context_wrapper():
    wrapped = wrap_untrusted_text("Engram memory search results", "hello")
    assert "<<<UNTRUSTED_SOURCE_DATA>>>" in wrapped
    assert "UNTRUSTED SOURCE DATA" in wrapped
    assert "hello" in wrapped


def test_intent_type_multiplier_boost():
    result = {"item_type": "mistake", "is_semantic": False}
    base = calculate_utility_score(result, inferred_type="pattern")
    boosted = calculate_utility_score(result, inferred_type="mistake")
    assert boosted > base


def test_gc_safety_guardrail(test_db):
    conn = test_db["conn"]
    for i in range(10):
        conn.execute(
            "INSERT INTO mistakes (date, context, mistake, fix, usage_count, created_at) "
            "VALUES ('2020-01-01', 'ctx', ?, 'fix', 0, '2020-01-01')",
            (f"mistake {i}",),
        )
    conn.commit()

    result = run_gc(mode="archive", days_unused=180, item_types=["mistake"], db_path=test_db["path"])
    assert result.get("blocked") is True
    assert result["processed"] == 0


def test_consolidation_fingerprint_skip(test_db):
    clusters, reason = find_consolidation_candidates(db_path=test_db["path"])
    assert reason != "unchanged" or clusters == []

    clusters2, reason2 = find_consolidation_candidates(db_path=test_db["path"])
    assert reason2 == "unchanged"
    assert clusters2 == []


def test_run_sleep_empty_db_reports_zero_clusters(test_db):
    # Regression: the (clusters, skip_reason) tuple was measured with len(),
    # so an empty DB reported clusters_found == 2.
    summary = run_sleep(dry_run=True, db_path=test_db["path"])
    assert summary["clusters_found"] == 0
    assert summary["items_invalidated"] == 0


def test_run_sleep_consolidates_cluster(test_db):
    conn = test_db["conn"]
    conn.execute(
        "INSERT INTO mistakes (date, context, mistake, fix) VALUES ('2026-01-01', 'ctx', 'dup a', 'fix')"
    )
    conn.execute(
        "INSERT INTO mistakes (date, context, mistake, fix) VALUES ('2026-01-01', 'ctx', 'dup b', 'fix')"
    )
    index_in_fts(conn, "mistake", 1, "dup a", "duplicate mistake", [])
    index_in_fts(conn, "mistake", 2, "dup b", "duplicate mistake", [])
    conn.commit()

    fake_cluster = {
        "item_type": "mistake",
        "cluster_size": 2,
        "avg_similarity": 0.95,
        "items": [
            {"item_type": "mistake", "item_id": 1, "title": "dup a"},
            {"item_type": "mistake", "item_id": 2, "title": "dup b"},
        ],
    }
    with patch(
        "src.maintenance.find_consolidation_candidates",
        return_value=([fake_cluster], None),
    ):
        summary = run_sleep(dry_run=False, db_path=test_db["path"])

    assert summary["clusters_found"] == 1
    assert summary["items_invalidated"] == 1


def test_health_report_capture_reuse_metric(test_db):
    from src.maintenance import run_health_check

    conn = test_db["conn"]
    # Two old memories: one reused, one never touched; one too-recent memory.
    conn.execute(
        "INSERT INTO mistakes (date, context, mistake, fix, usage_count, created_at) "
        "VALUES ('2026-01-01', 'c', 'reused one', 'f', 3, '2026-01-01')"
    )
    conn.execute(
        "INSERT INTO mistakes (date, context, mistake, fix, usage_count, created_at) "
        "VALUES ('2026-01-01', 'c', 'never used', 'f', 0, '2026-01-01')"
    )
    conn.execute(
        "INSERT INTO mistakes (date, context, mistake, fix, usage_count, created_at) "
        "VALUES ('2026-07-01', 'c', 'too recent', 'f', 0, datetime('now'))"
    )
    conn.commit()

    report = run_health_check(db_path=test_db["path"])
    cr = report["capture_reuse"]
    assert cr["eligible_30d_plus"] == 2
    assert cr["reused"] == 1
    assert cr["reuse_rate"] == 0.5
    assert report["items"]["mistake"]["reuse_rate_30d_plus"] == 0.5


def test_health_recommends_on_low_reuse(test_db):
    from src.maintenance import run_health_check

    conn = test_db["conn"]
    for i in range(12):
        conn.execute(
            "INSERT INTO mistakes (date, context, mistake, fix, usage_count, created_at) "
            f"VALUES ('2026-01-01', 'c', 'stale {i}', 'f', 0, '2026-01-01')"
        )
    conn.commit()

    report = run_health_check(db_path=test_db["path"])
    assert any("reused" in r for r in report["recommendations"])
