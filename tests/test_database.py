
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


def test_deferred_embedding_marks_pending(test_db, monkeypatch):
    """ENGRAM_DEFER_EMBED=1 skips inline embedding; row lands as pending."""
    from unittest.mock import patch

    monkeypatch.setenv("ENGRAM_DEFER_EMBED", "1")
    conn = test_db["conn"]
    conn.execute(
        "INSERT INTO skills (name, domain, trigger_desc, workflow) VALUES ('d', 'x', 't', 'w')"
    )
    with patch("src.database.embed_text") as embed:
        index_in_fts(conn, "skill", 1, "d", "t w", [])
    embed.assert_not_called()
    conn.commit()

    row = conn.execute(
        "SELECT status FROM embedding_status WHERE item_type='skill' AND item_id=1"
    ).fetchone()
    assert row["status"] == "pending"


def test_reembed_stale_uses_batch(test_db, monkeypatch):
    from unittest.mock import patch

    from src.database import reembed_stale

    monkeypatch.setenv("ENGRAM_DEFER_EMBED", "1")
    conn = test_db["conn"]
    for i in range(1, 4):
        conn.execute(
            "INSERT INTO skills (name, domain, trigger_desc, workflow) VALUES (?, 'x', 't', 'w')",
            (f"s{i}",),
        )
        index_in_fts(conn, "skill", i, f"s{i}", "t w", [])
    conn.commit()
    monkeypatch.delenv("ENGRAM_DEFER_EMBED")

    with patch("src.database.embed_batch", return_value=[[0.1] * 768] * 3) as batch:
        result = reembed_stale(db_path=test_db["path"], batch_size=50)

    assert batch.call_count == 1
    assert len(batch.call_args[0][0]) == 3
    assert result["succeeded"] == 3
    assert result["remaining"] == 0


def test_vec_load_failure_degrades_to_lexical(test_db, monkeypatch):
    """A vec0 dylib load failure (e.g. macOS TCC) must not kill connections."""
    from unittest.mock import patch

    import src.database as db
    from src.search import search

    with patch.object(db.sqlite_vec, "load", side_effect=OSError("dlopen blocked")):
        conn = test_db["conn"]
        conn.execute(
            "INSERT INTO skills (name, domain, trigger_desc, workflow) VALUES ('lex', 'd', 'lexical fallback', 'w')"
        )
        index_in_fts(conn, "skill", 1, "lex", "lexical fallback works", [])
        conn.commit()

        results = search("lexical fallback", db_path=test_db["path"])
        assert len(results) >= 1
        assert results[0]["is_semantic"] is False


def test_backup_export_includes_reflexes(test_db, monkeypatch):
    """Reflex scripts live only in the DB — backups must carry them."""
    from unittest.mock import patch

    from src.backup import export_to_json
    from src.reflex import approve_reflex, promote_skill, run_reflex

    conn = test_db["conn"]
    conn.execute(
        "INSERT INTO skills (name, domain, trigger_desc, workflow) VALUES ('bk', 'ops', 't', 'w')"
    )
    conn.commit()
    with patch("src.llm.is_llm_available", return_value=False):
        r = promote_skill(1, db_path=test_db["path"])
    conn.execute("UPDATE reflexes SET script = 'echo backed-up' WHERE id = ?", (r["id"],))
    conn.commit()
    approve_reflex(r["id"], db_path=test_db["path"])
    run_reflex(r["id"], db_path=test_db["path"])

    data = export_to_json(conn)
    assert "reflexes" in data and len(data["reflexes"]) == 1
    assert data["reflexes"][0]["script"] == "echo backed-up"
    assert "reflex_runs" in data and len(data["reflex_runs"]) == 1


def test_fts_porter_stemming_matches_morphological_variants(test_db):
    """'committing'/'verification' in docs must match 'commit'/'verify' queries."""
    conn = test_db["conn"]
    conn.execute(
        "INSERT INTO skills (name, domain, trigger_desc, workflow) "
        "VALUES ('Verification Pass', 'ops', 'before committing changes', 'run checks')"
    )
    index_in_fts(conn, "skill", 1, "Verification Pass", "before committing changes | run checks", [])
    conn.commit()

    rows = conn.execute(
        "SELECT item_id FROM memory_fts WHERE memory_fts MATCH ?", ('"commit" OR "verify"',)
    ).fetchall()
    assert len(rows) == 1


def test_v16_migration_preserves_vec_rowid_alignment(tmp_path):
    import json

    from src.database import get_connection, init_db

    db = str(tmp_path / "porter.db")
    init_db(db)
    with get_connection(db) as conn:
        conn.execute(
            "INSERT INTO skills (name, domain, trigger_desc, workflow) VALUES ('s', 'd', 'committing', 'w')"
        )
        cur = conn.execute(
            "INSERT INTO memory_fts (item_type, item_id, title, content, tags) VALUES ('skill','1','s','committing changes','')"
        )
        rowid = cur.lastrowid
        conn.execute(
            "INSERT INTO vec_memory(rowid, embedding) VALUES (?, ?)", (rowid, json.dumps([0.1] * 768))
        )
        conn.execute("UPDATE schema_meta SET value='15' WHERE key='version'")

    with get_connection(db) as conn:  # reopen → v16 migration runs
        r = conn.execute("SELECT rowid FROM memory_fts WHERE memory_fts MATCH '\"commit\"'").fetchone()
        assert r is not None and r["rowid"] == rowid
        v = conn.execute("SELECT rowid FROM vec_memory WHERE rowid = ?", (rowid,)).fetchone()
        assert v is not None
