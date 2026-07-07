
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


def test_migrate_embeddings_to_new_dimension(test_db, monkeypatch):
    """Switching to a model with a different output dim rebuilds vec_memory."""
    import json
    from unittest.mock import patch

    from src.database import get_connection, get_vec_dimension, migrate_embeddings_to_model

    monkeypatch.setenv("ENGRAM_EMBED_MODEL", "nomic-embed-text")
    conn = test_db["conn"]
    conn.execute(
        "INSERT INTO skills (name, domain, trigger_desc, workflow) VALUES ('s', 'd', 't', 'w')"
    )
    index_in_fts(conn, "skill", 1, "s", "t w", [])
    conn.commit()

    assert get_vec_dimension(db_path=test_db["path"]) == 768

    with patch("src.embeddings.embed_text", return_value=[0.1] * 512), \
         patch("src.database.embed_text", return_value=[0.1] * 512):
        result = migrate_embeddings_to_model("custom-512-model", db_path=test_db["path"])

    assert result["ok"] is True
    assert result["dimension"] == 512
    assert result["vec_table_rebuilt"] is True
    assert get_vec_dimension(db_path=test_db["path"]) == 512

    # The rebuilt table accepts 512-dim vectors
    with get_connection(test_db["path"]) as c:
        c.execute(
            "INSERT INTO vec_memory(rowid, embedding) VALUES (?, ?)",
            (999, json.dumps([0.2] * 512)),
        )


def test_get_vec_dimension_default(test_db):
    from src.database import get_vec_dimension

    assert get_vec_dimension(db_path=test_db["path"]) == 768
