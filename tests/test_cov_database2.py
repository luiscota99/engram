"""Coverage tests for src/database.py — targets the previously-uncovered
helpers (item fetch, projects, pins, dedup, fingerprints, schema_meta,
embedding maintenance). External embedding/subprocess I/O is mocked at the
module boundary so these run hermetically and fast."""

import json
from unittest.mock import patch

import pytest

import src.database as db
from src.database import (
    _jaccard_similarity,
    _resolve_git_root,
    check_duplicate_before_add,
    delete_item,
    ensure_tag,
    find_similar,
    get_connection,
    get_consolidation_fingerprint,
    get_embedding_stats,
    get_item,
    get_or_create_project,
    get_pinned_items,
    get_project_affinities,
    get_schema_meta,
    get_session_details,
    get_stored_consolidation_fingerprint,
    get_tags_for_item,
    get_vec_dimension,
    index_in_fts,
    is_pinned,
    link_item_to_project,
    link_tags,
    mark_embeddings_stale,
    migrate_embeddings_to_model,
    pin_item,
    rebuild_fts,
    rebuild_vec_table,
    record_usage,
    reembed_stale,
    save_consolidation_fingerprint,
    set_schema_meta,
    unpin_item,
    verify_embedding_schema_match,
)


def _seed_skill(conn, name="s1", domain="ops", trigger="t", workflow="w"):
    cur = conn.execute(
        "INSERT INTO skills (name, domain, trigger_desc, workflow) VALUES (?, ?, ?, ?)",
        (name, domain, trigger, workflow),
    )
    return cur.lastrowid


# ------------------------------------------------------------ tags helpers

def test_ensure_tag_normalizes_and_dedups(test_db):
    conn = test_db["conn"]
    first = ensure_tag(conn, "  MixedCase  ")
    # Same tag (normalized to lower/stripped) returns the same id.
    second = ensure_tag(conn, "mixedcase")
    assert first == second
    row = conn.execute("SELECT name FROM tags WHERE id=?", (first,)).fetchone()
    assert row["name"] == "mixedcase"


def test_link_tags_skips_blank_and_get_tags(test_db):
    conn = test_db["conn"]
    sid = _seed_skill(conn, name="tagged")
    link_tags(conn, "skill", sid, ["real", "  ", ""])
    conn.commit()
    assert get_tags_for_item(conn, "skill", sid) == ["real"]


# ------------------------------------------------------------ index_in_fts

def test_index_in_fts_ready_path_writes_vec_and_status(test_db, monkeypatch):
    monkeypatch.delenv("ENGRAM_DEFER_EMBED", raising=False)
    conn = test_db["conn"]
    with patch("src.database.embed_text", return_value=[0.1] * 768), \
         patch("src.database.resolve_embedding_model_name", return_value="mdl"), \
         patch("src.database.embedding_matches_vec_schema", return_value=(True, None)):
        index_in_fts(conn, "skill", 1, "Title", "content body", ["tg"])
    conn.commit()

    fts_row = conn.execute(
        "SELECT rowid FROM memory_fts WHERE item_type='skill' AND item_id='1'"
    ).fetchone()
    rowid = fts_row["rowid"]
    assert conn.execute("SELECT COUNT(*) AS c FROM vec_memory WHERE rowid=?", (rowid,)).fetchone()["c"] == 1
    status = conn.execute("SELECT status, embedding_model FROM embedding_status WHERE fts_rowid=?", (rowid,)).fetchone()
    assert status["status"] == "ready"
    assert status["embedding_model"] == "mdl"


def test_index_in_fts_schema_mismatch_marks_failed(test_db, monkeypatch):
    monkeypatch.delenv("ENGRAM_DEFER_EMBED", raising=False)
    conn = test_db["conn"]
    with patch("src.database.embed_text", return_value=[0.1] * 768), \
         patch("src.database.resolve_embedding_model_name", return_value="mdl"), \
         patch("src.database.embedding_matches_vec_schema", return_value=(False, "wrong dim")):
        index_in_fts(conn, "skill", 2, "T2", "body", [])
    conn.commit()

    rowid = conn.execute("SELECT rowid FROM memory_fts WHERE item_id='2'").fetchone()["rowid"]
    status = conn.execute("SELECT status, error_message FROM embedding_status WHERE fts_rowid=?", (rowid,)).fetchone()
    assert status["status"] == "failed"
    assert status["error_message"] == "wrong dim"


def test_index_in_fts_reindex_replaces_old_row(test_db, monkeypatch):
    monkeypatch.setenv("ENGRAM_DEFER_EMBED", "1")
    conn = test_db["conn"]
    index_in_fts(conn, "skill", 3, "First", "first body", [])
    first_rowid = conn.execute("SELECT rowid FROM memory_fts WHERE item_id='3'").fetchone()["rowid"]
    index_in_fts(conn, "skill", 3, "Second", "second body", [])
    conn.commit()

    rows = conn.execute("SELECT rowid, title FROM memory_fts WHERE item_id='3'").fetchall()
    assert len(rows) == 1
    assert rows[0]["title"] == "Second"
    assert first_rowid is not None


# ---------------------------------------------------------------- get_item

def test_get_item_returns_row_with_tags(test_db):
    conn = test_db["conn"]
    sid = _seed_skill(conn, name="fetchme")
    db.link_tags(conn, "skill", sid, ["alpha", "beta"])
    conn.commit()

    item = get_item("skill", sid, db_path=test_db["path"])
    assert item is not None
    assert item["name"] == "fetchme"
    assert sorted(item["tags"]) == ["alpha", "beta"]


def test_get_item_unknown_type_returns_none(test_db):
    assert get_item("nonsense", 1, db_path=test_db["path"]) is None


def test_get_item_missing_id_returns_none(test_db):
    assert get_item("skill", 99999, db_path=test_db["path"]) is None


# ------------------------------------------------------ get_session_details

def test_get_session_details_includes_transcripts(test_db):
    conn = test_db["conn"]
    conn.execute(
        "INSERT INTO sessions (session_id, title, date, domain) VALUES (?, ?, ?, ?)",
        ("sess-1", "My Session", "2026-01-01", "ops"),
    )
    conn.execute(
        "INSERT INTO session_transcripts (session_id, role, content) VALUES (?, ?, ?)",
        ("sess-1", "analyst", "first"),
    )
    conn.execute(
        "INSERT INTO session_transcripts (session_id, role, content) VALUES (?, ?, ?)",
        ("sess-1", "critic", "second"),
    )
    conn.commit()

    details = get_session_details("sess-1", db_path=test_db["path"])
    assert details["title"] == "My Session"
    assert [t["role"] for t in details["transcripts"]] == ["analyst", "critic"]
    assert details["transcripts"][0]["content"] == "first"


def test_get_session_details_missing_returns_none(test_db):
    assert get_session_details("nope", db_path=test_db["path"]) is None


# ------------------------------------------------------------- record_usage

def test_record_usage_increments_count(test_db):
    conn = test_db["conn"]
    sid = _seed_skill(conn)
    conn.commit()

    assert record_usage("skill", sid, db_path=test_db["path"]) is True
    with get_connection(test_db["path"]) as c:
        row = c.execute("SELECT usage_count, last_used_at FROM skills WHERE id = ?", (sid,)).fetchone()
    assert row["usage_count"] == 1
    assert row["last_used_at"] is not None


def test_record_usage_unknown_type_returns_false(test_db):
    assert record_usage("bogus", 1, db_path=test_db["path"]) is False


def test_record_usage_fails_soft_when_db_locked(test_db):
    """An incidental counter bump must never block the turn: under a held write
    lock it returns False (short busy_timeout) instead of stalling ~10s."""
    import time

    conn = test_db["conn"]
    sid = _seed_skill(conn)
    conn.commit()

    # Hold the write lock from a competing connection so record_usage's UPDATE
    # can't acquire it and hits its 500ms busy_timeout.
    holder = db.sqlite3.connect(test_db["path"])
    try:
        holder.execute("PRAGMA journal_mode=WAL")
        holder.execute("BEGIN IMMEDIATE")
        holder.execute(
            "UPDATE skills SET usage_count = usage_count + 1 WHERE id = ?", (sid,)
        )

        start = time.monotonic()
        result = record_usage("skill", sid, db_path=test_db["path"])
        elapsed = time.monotonic() - start
    finally:
        holder.rollback()
        holder.close()

    assert result is False
    # Well under the 10s default; the short busy_timeout is what makes it soft.
    assert elapsed < 5.0


# --------------------------------------------------------- _resolve_git_root

def test_resolve_git_root_uses_git_toplevel(monkeypatch):
    db._git_root_cache.clear()

    class _R:
        returncode = 0
        stdout = "/repo/root\n"

    with patch("subprocess.run", return_value=_R()) as run:
        first = _resolve_git_root("/repo/root/sub")
        second = _resolve_git_root("/repo/root/sub")
    assert first == "/repo/root"
    assert second == "/repo/root"
    # Result is memoized: subprocess only runs once for the same path.
    assert run.call_count == 1
    db._git_root_cache.clear()


def test_resolve_git_root_nonzero_returns_path_as_is(monkeypatch):
    db._git_root_cache.clear()

    class _R:
        returncode = 128
        stdout = ""

    with patch("subprocess.run", return_value=_R()):
        assert _resolve_git_root("/not/a/repo") == "/not/a/repo"
    db._git_root_cache.clear()


def test_resolve_git_root_exception_returns_path(monkeypatch):
    db._git_root_cache.clear()
    with patch("subprocess.run", side_effect=OSError("git missing")):
        assert _resolve_git_root("/some/path") == "/some/path"
    db._git_root_cache.clear()


# --------------------------------------------------------- project helpers

def test_get_or_create_project_creates_then_fetches(test_db):
    with patch("src.database._resolve_git_root", side_effect=lambda p: p):
        created = get_or_create_project("/tmp/myproj", db_path=test_db["path"])
        assert created["name"] == "myproj"
        assert created["path"] == "/tmp/myproj"
        pid = created["id"]

        again = get_or_create_project("/tmp/myproj", db_path=test_db["path"])
    assert again["id"] == pid
    assert again["path"] == "/tmp/myproj"


def test_get_or_create_project_uses_explicit_name(test_db):
    with patch("src.database._resolve_git_root", side_effect=lambda p: p):
        created = get_or_create_project("/tmp/proj2", name="Custom", db_path=test_db["path"])
    assert created["name"] == "Custom"


def test_link_item_to_project_inserts(test_db):
    conn = test_db["conn"]
    with patch("src.database._resolve_git_root", side_effect=lambda p: p):
        proj = get_or_create_project("/tmp/lp", conn=conn)
    conn.commit()
    assert link_item_to_project("skill", 5, proj["id"], affinity="created", db_path=test_db["path"]) is True

    with get_connection(test_db["path"]) as c:
        row = c.execute(
            "SELECT affinity FROM item_projects WHERE item_type='skill' AND item_id=5 AND project_id=?",
            (proj["id"],),
        ).fetchone()
    assert row["affinity"] == "created"


def test_get_project_affinities_empty_inputs(test_db):
    assert get_project_affinities([], 1, db_path=test_db["path"]) == {}
    assert get_project_affinities([{"item_type": "skill", "item_id": 1}], None, db_path=test_db["path"]) == {}


def test_get_project_affinities_batch(test_db):
    conn = test_db["conn"]
    conn.execute("INSERT INTO projects (name, path) VALUES ('p', '/p')")
    pid = conn.execute("SELECT id FROM projects WHERE path='/p'").fetchone()["id"]
    conn.execute(
        "INSERT INTO item_projects (item_type, item_id, project_id, affinity) VALUES ('skill', 1, ?, 'used')",
        (pid,),
    )
    conn.execute(
        "INSERT INTO item_projects (item_type, item_id, project_id, affinity) VALUES ('mistake', 2, ?, 'created')",
        (pid,),
    )
    conn.commit()

    results = [
        {"item_type": "skill", "item_id": 1},
        {"item_type": "mistake", "item_id": 2},
        {"item_type": "skill", "item_id": 99},  # no affinity row
    ]
    aff = get_project_affinities(results, pid, conn=conn)
    assert aff[("skill", 1)] == "used"
    assert aff[("mistake", 2)] == "created"
    assert ("skill", 99) not in aff


# --------------------------------------------------------------- find_similar

def _seed_vec_row(conn, item_type, item_id, title, content, vector):
    cur = conn.execute(
        "INSERT INTO memory_fts (item_type, item_id, title, content, tags) VALUES (?, ?, ?, ?, '')",
        (item_type, str(item_id), title, content),
    )
    rowid = cur.lastrowid
    conn.execute(
        "INSERT INTO vec_memory(rowid, embedding) VALUES (?, ?)",
        (rowid, json.dumps(vector)),
    )
    return rowid


def test_find_similar_returns_matches(test_db):
    conn = test_db["conn"]
    vec = [0.1] * 768
    _seed_vec_row(conn, "skill", 1, "Vector Title", "vector body text here", vec)
    conn.commit()

    with patch("src.embeddings.embed_text", return_value=vec), \
         patch("src.database.resolve_embedding_model_name", return_value="m"), \
         patch("src.database.embedding_matches_vec_schema", return_value=(True, None)):
        results = find_similar("query text", item_type="skill", threshold=0.5, db_path=test_db["path"])

    assert len(results) == 1
    hit = results[0]
    assert hit["item_type"] == "skill"
    assert hit["title"] == "Vector Title"
    assert hit["snippet"].startswith("vector body")
    assert hit["similarity"] >= 0.5


def test_find_similar_no_embedding_returns_empty(test_db):
    with patch("src.embeddings.embed_text", return_value=None):
        assert find_similar("x", db_path=test_db["path"]) == []


def test_find_similar_schema_mismatch_returns_empty(test_db):
    with patch("src.embeddings.embed_text", return_value=[0.1] * 768), \
         patch("src.database.resolve_embedding_model_name", return_value="m"), \
         patch("src.database.embedding_matches_vec_schema", return_value=(False, "dim mismatch")):
        assert find_similar("x", db_path=test_db["path"]) == []


# ---------------------------------------------------------- embedding stats

def test_get_embedding_stats_counts(test_db):
    conn = test_db["conn"]
    statuses = ["ready", "ready", "stale", "pending", "failed"]
    for i, st in enumerate(statuses, start=1):
        conn.execute(
            "INSERT INTO embedding_status (fts_rowid, item_type, item_id, status) VALUES (?, 'skill', ?, ?)",
            (i, i, st),
        )
    conn.commit()

    with patch("src.database.resolve_embedding_model_name", return_value="the-model"):
        stats = get_embedding_stats(db_path=test_db["path"])
    assert stats["model"] == "the-model"
    assert stats["total"] == 5
    assert stats["ready"] == 2
    assert stats["stale"] == 1
    assert stats["pending"] == 1
    assert stats["failed"] == 1


def test_mark_embeddings_stale_flips_ready(test_db):
    conn = test_db["conn"]
    conn.execute("INSERT INTO embedding_status (fts_rowid, item_type, item_id, status) VALUES (1, 'skill', 1, 'ready')")
    conn.execute("INSERT INTO embedding_status (fts_rowid, item_type, item_id, status) VALUES (2, 'skill', 2, 'ready')")
    conn.execute("INSERT INTO embedding_status (fts_rowid, item_type, item_id, status) VALUES (3, 'skill', 3, 'pending')")
    conn.commit()

    count = mark_embeddings_stale(db_path=test_db["path"])
    assert count == 2
    with get_connection(test_db["path"]) as c:
        stale = c.execute("SELECT COUNT(*) AS c FROM embedding_status WHERE status='stale'").fetchone()["c"]
    assert stale == 2


# --------------------------------------------------------------- delete_item

def test_delete_item_removes_everything(test_db, monkeypatch):
    monkeypatch.setenv("ENGRAM_DEFER_EMBED", "1")
    conn = test_db["conn"]
    sid = _seed_skill(conn, name="delme")
    db.link_tags(conn, "skill", sid, ["tg"])
    index_in_fts(conn, "skill", sid, "delme", "content", ["tg"])
    conn.commit()

    delete_item(conn, "skill", sid)
    conn.commit()

    assert conn.execute("SELECT COUNT(*) AS c FROM skills WHERE id=?", (sid,)).fetchone()["c"] == 0
    assert conn.execute(
        "SELECT COUNT(*) AS c FROM item_tags WHERE item_type='skill' AND item_id=?", (sid,)
    ).fetchone()["c"] == 0
    assert conn.execute(
        "SELECT COUNT(*) AS c FROM memory_fts WHERE item_type='skill' AND item_id=?", (str(sid),)
    ).fetchone()["c"] == 0


def test_delete_item_unknown_type_raises(test_db):
    with pytest.raises(ValueError, match="Unknown item type"):
        delete_item(test_db["conn"], "bogus", 1)


# ------------------------------------------------------------------- pinning

def test_pin_and_unpin_and_is_pinned(test_db):
    conn = test_db["conn"]
    sid = _seed_skill(conn, name="pinme")
    conn.commit()

    assert pin_item("skill", sid, db_path=test_db["path"]) is True
    assert is_pinned("skill", sid, db_path=test_db["path"]) is True
    assert unpin_item("skill", sid, db_path=test_db["path"]) is True
    # Second unpin: nothing to delete.
    assert unpin_item("skill", sid, db_path=test_db["path"]) is False
    assert is_pinned("skill", sid, db_path=test_db["path"]) is False


def test_pin_item_rejects_unpinnable_type(test_db):
    with pytest.raises(ValueError, match="Cannot pin item type"):
        pin_item("session", 1, db_path=test_db["path"])


def test_pin_item_missing_item_returns_false(test_db):
    assert pin_item("skill", 424242, db_path=test_db["path"]) is False


def test_get_pinned_items(test_db):
    conn = test_db["conn"]
    conn.execute(
        "INSERT INTO memory_fts (item_type, item_id, title, content, tags) VALUES ('skill', '1', 'Pinned Title', 'body', 'x y')"
    )
    conn.execute("INSERT INTO item_pins (item_type, item_id) VALUES ('skill', 1)")
    conn.commit()

    items = get_pinned_items(conn=conn)
    assert len(items) == 1
    it = items[0]
    assert it["title"] == "Pinned Title"
    assert it["pinned"] is True
    assert it["utility_score"] == 9999.0
    assert it["is_semantic"] is False
    assert it["item_id"] == "1"

    # item_type filter that matches nothing.
    assert get_pinned_items(item_type="mistake", conn=conn) == []


# ---------------------------------------------------------- jaccard / dedup

def test_jaccard_similarity():
    assert _jaccard_similarity("the quick brown fox", "the quick brown fox") == 1.0
    assert _jaccard_similarity("", "anything") == 0.0
    # 2 shared of 3 total unique tokens.
    assert _jaccard_similarity("a b", "a b c") == pytest.approx(2 / 3)


def test_check_duplicate_empty_inputs(test_db):
    result = check_duplicate_before_add("", "skill", db_path=test_db["path"])
    assert result == {"duplicates": [], "exact_match": False, "fuzzy_match": False}


def test_check_duplicate_exact_name(test_db):
    conn = test_db["conn"]
    _seed_skill(conn, name="Exact Name")
    conn.commit()

    result = check_duplicate_before_add(
        "some content", "skill", name="exact name", db_path=test_db["path"]
    )
    assert result["exact_match"] is True
    assert result["duplicates"][0]["match_kind"] == "exact_name"
    assert result["duplicates"][0]["similarity"] == 1.0


def test_check_duplicate_vector_match(test_db):
    fake_hits = [{"item_type": "skill", "item_id": 1, "title": "t", "similarity": 0.9}]
    with patch("src.database.find_similar", return_value=fake_hits):
        result = check_duplicate_before_add("content here", "skill", db_path=test_db["path"])
    assert result["duplicates"][0]["match_kind"] == "vector"
    assert result["duplicates"][0]["similarity"] == 0.9


def test_check_duplicate_jaccard_fuzzy(test_db):
    conn = test_db["conn"]
    conn.execute(
        "INSERT INTO memory_fts (item_type, item_id, title, content, tags) "
        "VALUES ('skill', '1', 'login bug', 'auth token expires early on refresh', '')"
    )
    conn.commit()

    content = "auth token expires early on refresh"
    with patch("src.database.find_similar", return_value=[]):
        result = check_duplicate_before_add(content, "skill", db_path=test_db["path"])
    assert result["fuzzy_match"] is True
    assert result["duplicates"][0]["match_kind"] == "jaccard"
    assert result["duplicates"][0]["similarity"] >= 0.6


# --------------------------------------------------- consolidation fingerprint

def test_consolidation_fingerprint_is_stable_and_content_sensitive(test_db):
    conn = test_db["conn"]
    conn.execute(
        "INSERT INTO memory_fts (item_type, item_id, title, content, tags) VALUES ('mistake', '1', 'A', 'x', '')"
    )
    conn.commit()

    fp1 = get_consolidation_fingerprint(["mistake"], db_path=test_db["path"])
    fp2 = get_consolidation_fingerprint(["mistake"], db_path=test_db["path"])
    assert fp1 == fp2
    assert len(fp1) == 64  # sha256 hex

    conn.execute(
        "INSERT INTO memory_fts (item_type, item_id, title, content, tags) VALUES ('mistake', '2', 'B', 'y', '')"
    )
    conn.commit()
    fp3 = get_consolidation_fingerprint(["mistake"], db_path=test_db["path"])
    assert fp3 != fp1


def test_stored_consolidation_fingerprint_roundtrip(test_db):
    assert get_stored_consolidation_fingerprint(db_path=test_db["path"]) is None
    save_consolidation_fingerprint("abc123", db_path=test_db["path"])
    assert get_stored_consolidation_fingerprint(db_path=test_db["path"]) == "abc123"
    # Upsert overwrites.
    save_consolidation_fingerprint("def456", db_path=test_db["path"])
    assert get_stored_consolidation_fingerprint(db_path=test_db["path"]) == "def456"


# ------------------------------------------------------------- schema_meta

def test_schema_meta_roundtrip_and_upsert(test_db):
    assert get_schema_meta("nope", db_path=test_db["path"]) is None
    set_schema_meta("mykey", "v1", db_path=test_db["path"])
    assert get_schema_meta("mykey", db_path=test_db["path"]) == "v1"
    set_schema_meta("mykey", "v2", db_path=test_db["path"])
    assert get_schema_meta("mykey", db_path=test_db["path"]) == "v2"


# ------------------------------------------------------------ get_vec_dimension

def test_get_vec_dimension_reads_stored(test_db):
    set_schema_meta("vec_dimension", "384", db_path=test_db["path"])
    assert get_vec_dimension(db_path=test_db["path"]) == 384


def test_get_vec_dimension_exception_falls_back(test_db):
    class _Broken:
        def execute(self, *a, **k):
            raise RuntimeError("boom")

    assert get_vec_dimension(conn=_Broken()) == 768


# ----------------------------------------------------------- rebuild_vec_table

def test_rebuild_vec_table_changes_dimension(test_db):
    conn = test_db["conn"]
    rebuild_vec_table(256, conn)
    conn.execute("INSERT INTO vec_memory(rowid, embedding) VALUES (1, ?)", (json.dumps([0.5] * 256),))
    # A wrong-width vector is rejected by the rebuilt table.
    with pytest.raises(db.sqlite3.Error):
        conn.execute("INSERT INTO vec_memory(rowid, embedding) VALUES (2, ?)", (json.dumps([0.5] * 768),))


# ------------------------------------------------- verify_embedding_schema_match

def test_verify_embedding_schema_disabled_backend(test_db):
    with patch("src.embeddings.resolve_embed_backend", return_value=("disabled", "")):
        assert verify_embedding_schema_match(db_path=test_db["path"]) is None


def test_verify_embedding_schema_sets_default_when_missing(test_db):
    with patch("src.embeddings.resolve_embed_backend", return_value=("ollama", "url")):
        assert verify_embedding_schema_match(db_path=test_db["path"]) is None
    # It stored the default dimension.
    assert get_schema_meta("vec_dimension", db_path=test_db["path"]) == "768"


def test_verify_embedding_schema_mismatch_returns_error(test_db):
    set_schema_meta("vec_dimension", "768", db_path=test_db["path"])
    with patch("src.embeddings.resolve_embed_backend", return_value=("ollama", "url")), \
         patch("src.embeddings.embed_text", return_value=[0.1] * 512), \
         patch("src.embeddings.resolve_embedding_model_name", return_value="weird-model"):
        err = verify_embedding_schema_match(db_path=test_db["path"])
    assert err is not None
    assert "512-dim" in err
    assert "768" in err
    assert "weird-model" in err


def test_verify_embedding_schema_match_ok(test_db):
    set_schema_meta("vec_dimension", "768", db_path=test_db["path"])
    with patch("src.embeddings.resolve_embed_backend", return_value=("ollama", "url")), \
         patch("src.embeddings.embed_text", return_value=[0.1] * 768):
        assert verify_embedding_schema_match(db_path=test_db["path"]) is None


def test_verify_embedding_schema_probe_none(test_db):
    set_schema_meta("vec_dimension", "768", db_path=test_db["path"])
    with patch("src.embeddings.resolve_embed_backend", return_value=("ollama", "url")), \
         patch("src.embeddings.embed_text", return_value=None):
        assert verify_embedding_schema_match(db_path=test_db["path"]) is None


# --------------------------------------------------------------- rebuild_fts

def test_rebuild_fts_repopulates_index(test_db, monkeypatch):
    monkeypatch.setenv("ENGRAM_DEFER_EMBED", "1")
    conn = test_db["conn"]
    _seed_skill(conn, name="RebuildSkill", trigger="do a thing", workflow="steps")
    conn.execute(
        "INSERT INTO mistakes (date, context, mistake, fix) VALUES ('2026-01-01', 'ctx', 'oops', 'fixit')"
    )
    # Stale FTS row that rebuild must clear.
    conn.execute(
        "INSERT INTO memory_fts (item_type, item_id, title, content, tags) VALUES ('skill', '999', 'stale', 'gone', '')"
    )
    conn.commit()

    assert rebuild_fts(conn) is True
    conn.commit()

    # Stale row is gone; core rows are present.
    assert conn.execute(
        "SELECT COUNT(*) AS c FROM memory_fts WHERE item_id='999'"
    ).fetchone()["c"] == 0
    titles = {
        r["title"]
        for r in conn.execute("SELECT title FROM memory_fts").fetchall()
    }
    assert "RebuildSkill" in titles
    assert "oops" in titles


# --------------------------------------------------------------- reembed_stale

def test_reembed_stale_no_vec_extension(test_db):
    with patch("src.database.sqlite_vec", None):
        result = reembed_stale(db_path=test_db["path"])
    assert result["error"] == "sqlite_vec not available"
    assert result["processed"] == 0


def test_reembed_stale_missing_fts_row_marked_failed(test_db):
    conn = test_db["conn"]
    # embedding_status pointing at an FTS rowid that does not exist.
    conn.execute(
        "INSERT INTO embedding_status (fts_rowid, item_type, item_id, status) VALUES (55555, 'skill', 1, 'pending')"
    )
    conn.commit()

    with patch("src.database.embed_batch", return_value=[]):
        result = reembed_stale(db_path=test_db["path"])
    assert result["failed"] == 1
    with get_connection(test_db["path"]) as c:
        row = c.execute("SELECT status, error_message FROM embedding_status WHERE fts_rowid=55555").fetchone()
    assert row["status"] == "failed"
    assert row["error_message"] == "FTS row missing"


def test_reembed_stale_vec_mismatch_marked_failed(test_db):
    conn = test_db["conn"]
    cur = conn.execute(
        "INSERT INTO memory_fts (item_type, item_id, title, content, tags) VALUES ('skill', '1', 'T', 'body', '')"
    )
    rowid = cur.lastrowid
    conn.execute(
        "INSERT INTO embedding_status (fts_rowid, item_type, item_id, status) VALUES (?, 'skill', 1, 'pending')",
        (rowid,),
    )
    conn.commit()

    with patch("src.database.embed_batch", return_value=[[0.1] * 768]), \
         patch("src.database.resolve_embedding_model_name", return_value="m"), \
         patch("src.database.embedding_matches_vec_schema", return_value=(False, "bad dim")):
        result = reembed_stale(db_path=test_db["path"])
    assert result["failed"] == 1
    with get_connection(test_db["path"]) as c:
        row = c.execute("SELECT status, error_message FROM embedding_status WHERE fts_rowid=?", (rowid,)).fetchone()
    assert row["status"] == "failed"
    assert row["error_message"] == "bad dim"


def test_reembed_stale_success_path(test_db):
    conn = test_db["conn"]
    cur = conn.execute(
        "INSERT INTO memory_fts (item_type, item_id, title, content, tags) VALUES ('skill', '7', 'OK', 'good body', 'tg')"
    )
    rowid = cur.lastrowid
    conn.execute(
        "INSERT INTO embedding_status (fts_rowid, item_type, item_id, status) VALUES (?, 'skill', 7, 'pending')",
        (rowid,),
    )
    conn.commit()

    with patch("src.database.embed_batch", return_value=[[0.2] * 768]), \
         patch("src.database.resolve_embedding_model_name", return_value="m"), \
         patch("src.database.embedding_matches_vec_schema", return_value=(True, None)):
        result = reembed_stale(db_path=test_db["path"])
    assert result["succeeded"] == 1
    assert result["remaining"] == 0
    with get_connection(test_db["path"]) as c:
        row = c.execute("SELECT status FROM embedding_status WHERE fts_rowid=?", (rowid,)).fetchone()
        vec = c.execute("SELECT COUNT(*) AS c FROM vec_memory WHERE rowid=?", (rowid,)).fetchone()
    assert row["status"] == "ready"
    assert vec["c"] == 1


def test_migrate_embeddings_same_dimension_no_rebuild(test_db, monkeypatch):
    monkeypatch.setenv("ENGRAM_EMBED_MODEL", "nomic-embed-text")
    conn = test_db["conn"]
    _seed_skill(conn, name="mg")
    index_in_fts(conn, "skill", 1, "mg", "body", [])
    conn.execute("UPDATE embedding_status SET status='pending'")
    conn.commit()

    with patch("src.embeddings.embed_text", return_value=[0.3] * 768), \
         patch("src.database.embed_batch", return_value=[[0.3] * 768]), \
         patch("src.database.resolve_embedding_model_name", return_value="same-768"), \
         patch("src.database.embedding_matches_vec_schema", return_value=(True, None)):
        result = migrate_embeddings_to_model("same-768", db_path=test_db["path"])

    assert result["ok"] is True
    assert result["dimension"] == 768
    assert result["vec_table_rebuilt"] is False
    assert get_schema_meta("embed_model", db_path=test_db["path"]) == "same-768"


def test_migrate_embeddings_probe_unreachable(test_db):
    with patch("src.embeddings.embed_text", return_value=None):
        result = migrate_embeddings_to_model("dead-model", db_path=test_db["path"])
    assert result["ok"] is False
    assert "probe" in result["error"]


def test_reembed_stale_empty_embedding_marked_failed(test_db):
    conn = test_db["conn"]
    cur = conn.execute(
        "INSERT INTO memory_fts (item_type, item_id, title, content, tags) VALUES ('skill', '2', 'T2', 'body2', '')"
    )
    rowid = cur.lastrowid
    conn.execute(
        "INSERT INTO embedding_status (fts_rowid, item_type, item_id, status) VALUES (?, 'skill', 2, 'stale')",
        (rowid,),
    )
    conn.commit()

    with patch("src.database.embed_batch", return_value=[None]), \
         patch("src.database.resolve_embedding_model_name", return_value="m"):
        result = reembed_stale(db_path=test_db["path"])
    assert result["failed"] == 1
    with get_connection(test_db["path"]) as c:
        row = c.execute("SELECT status, error_message FROM embedding_status WHERE fts_rowid=?", (rowid,)).fetchone()
    assert row["status"] == "failed"
    assert "Ollama unavailable" in row["error_message"]
