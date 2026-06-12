"""Tests for memory enhancements (pins, dedup, security, maintenance)."""

from __future__ import annotations

from src.database import (
    SCHEMA_VERSION,
    check_duplicate_before_add,
    get_pinned_items,
    index_in_fts,
    pin_item,
    unpin_item,
)
from src.maintenance import find_consolidation_candidates, run_gc
from src.prompt_security import wrap_untrusted_text
from src.ranking import calculate_utility_score
from src.search import search


def test_schema_version_is_10():
    assert SCHEMA_VERSION == 11


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
