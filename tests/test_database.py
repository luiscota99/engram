
from src.database import SCHEMA_VERSION, get_tags_for_item, index_in_fts, link_tags
from src.search import search


def test_database_initialization(test_db):
    """Test that the database initializes with the correct schema version."""
    row = test_db["conn"].execute("SELECT value FROM schema_meta WHERE key='version'").fetchone()
    assert row is not None
    assert int(row["value"]) == SCHEMA_VERSION

def test_tag_linking(test_db):
    """Test that tags are correctly linked to items."""
    conn = test_db["conn"]
    # Insert dummy mistake
    cursor = conn.execute(
        "INSERT INTO mistakes (date, context, mistake, fix) VALUES (?, ?, ?, ?)",
        ("2026-04-19", "Test context", "Test mistake", "Test fix")
    )
    mid = cursor.lastrowid

    link_tags(conn, "mistake", mid, ["test-tag", "pytest"])
    tags = get_tags_for_item(conn, "mistake", mid)

    assert "test-tag" in tags
    assert "pytest" in tags

def test_fts_indexing_and_search(test_db):
    """Test that lexical search correctly indexes and retrieves items."""
    conn = test_db["conn"]
    conn.execute("INSERT INTO memory_fts(memory_fts) VALUES('rebuild')")
    index_in_fts(conn, "skill", 1, "Testing FTS", "This is a test of the lexical search.", ["test-tag"])
    conn.commit()

    # Search for "lexical"
    results = search("lexical", db_path=test_db["path"])
    assert len(results) == 1
    assert results[0]["title"] == "Testing FTS"
    assert "test-tag" in results[0]["tags"]
