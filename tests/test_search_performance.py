"""
Performance regression tests for search.
Ensures search remains fast even with many items (guards against N+1 queries).
"""

import time

from src.database import index_in_fts, link_tags
from src.search import search


def test_search_performance_with_many_items(test_db):
    """Search with 100+ items must complete in < 500ms."""
    conn = test_db["conn"]

    # Seed 120 items across all types
    types_and_tables = [
        ("mistake", "mistakes", "date, context, mistake, fix", "'2026-04-20', 'ctx', 'mistake {}', 'fix {}'"),
        ("pattern", "patterns", "name, symptoms, root_cause, standard_fix", "'pattern_{}', 'symptom {}', 'cause {}', 'fix {}'"),
        ("skill", "skills", "name, domain, trigger_desc, workflow", "'skill_{}', 'domain', 'trigger {}', 'workflow {}'"),
        ("conversation", "conversations", "conversation_id, title, date, domain", "'conv_{}', 'conv title {}', '2026-04-20', 'domain'"),
        ("prompt", "prompts", "name, role, domain, description, prompt_text", "'prompt_{}', 'role', 'domain', 'desc {}', 'text {}'"),
    ]

    item_count = 0
    for item_type, table, columns, values_template in types_and_tables:
        for i in range(24):
            values = values_template.replace("{}", str(i))
            cursor = conn.execute(f"INSERT INTO {table} ({columns}) VALUES ({values})")
            item_id = cursor.lastrowid
            tags = [f"tag-{i % 5}", f"perf-test"]
            link_tags(conn, item_type, item_id, tags)
            index_in_fts(conn, item_type, item_id, f"{item_type} title {i}", f"content for {item_type} number {i} with searchable text", tags)
            item_count += 1

    conn.commit()
    assert item_count == 120, f"Expected 120 seeded items, got {item_count}"

    # Time the search
    start = time.perf_counter()
    results = search("searchable text", db_path=test_db["path"])
    elapsed_ms = (time.perf_counter() - start) * 1000

    assert elapsed_ms < 500, f"Search took {elapsed_ms:.0f}ms — exceeds 500ms budget (possible N+1 query)"
    assert len(results) > 0, "Search returned no results"


def test_utility_score_is_computed(test_db):
    """Every search result must have a utility_score field."""
    conn = test_db["conn"]
    conn.execute(
        "INSERT INTO skills (name, domain, trigger_desc, workflow) VALUES (?, ?, ?, ?)",
        ("test-skill", "testing", "when testing", "step 1, step 2"),
    )
    index_in_fts(conn, "skill", 1, "test-skill", "when testing step 1 step 2", ["testing"])
    conn.commit()

    results = search("testing", db_path=test_db["path"])
    assert len(results) >= 1
    for r in results:
        assert "utility_score" in r, f"Result missing utility_score: {r}"
        assert r["utility_score"] > 0, f"utility_score should be positive: {r}"


def test_utility_score_respects_usage_count(test_db):
    """Items with higher usage_count should score higher."""
    conn = test_db["conn"]

    # Insert two skills — one with high usage, one with none
    conn.execute(
        "INSERT INTO skills (name, domain, trigger_desc, workflow, usage_count) VALUES (?, ?, ?, ?, ?)",
        ("popular-skill", "testing", "trigger popular", "workflow popular", 10),
    )
    index_in_fts(conn, "skill", 1, "popular-skill", "trigger popular workflow popular", ["testing"])

    conn.execute(
        "INSERT INTO skills (name, domain, trigger_desc, workflow, usage_count) VALUES (?, ?, ?, ?, ?)",
        ("unused-skill", "testing", "trigger unused", "workflow unused", 0),
    )
    index_in_fts(conn, "skill", 2, "unused-skill", "trigger unused workflow unused", ["testing"])
    conn.commit()

    results = search("trigger workflow", db_path=test_db["path"])
    assert len(results) >= 2

    # Find both results
    popular = next((r for r in results if r["title"] == "popular-skill"), None)
    unused = next((r for r in results if r["title"] == "unused-skill"), None)

    assert popular is not None, "popular-skill not found in results"
    assert unused is not None, "unused-skill not found in results"
    assert popular["utility_score"] > unused["utility_score"], (
        f"popular ({popular['utility_score']}) should outscore unused ({unused['utility_score']})"
    )
