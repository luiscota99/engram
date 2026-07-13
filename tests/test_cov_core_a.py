"""Coverage tests for src/backup.py, src/migrations.py, src/search.py.

All tests use the function-scoped `test_db` fixture (conftest.py) or a local
in-memory sqlite connection. External I/O (git subprocess, Ollama embeddings)
is mocked at the module boundary so the suite is hermetic.
"""

import json
import math
import sqlite3
import struct
import subprocess
from unittest.mock import patch

import pytest

from src.backup import export_to_json, run_backup
from src.migrations import (
    _add_column_if_missing,
    _normalize_vec_memory,
    _rebuild_fts_with_porter,
    _swap_fts_table,
    backup_before_migration,
    downgrade_to,
    run_migrations,
)
from src.search import (
    _fts5_tag_phrase,
    _fts_query_terms,
    _get_stale_rowids,
    get_recent,
    get_stats,
    search,
    semantic_search,
)


def _seed_fts(conn, item_type, item_id, title, content, tags=""):
    """Insert one row directly into memory_fts and return its rowid."""
    cur = conn.execute(
        "INSERT INTO memory_fts (item_type, item_id, title, content, tags) "
        "VALUES (?, ?, ?, ?, ?)",
        (item_type, str(item_id), title, content, tags),
    )
    return cur.lastrowid


# ────────────────────────────── backup.py ──────────────────────────────


def test_export_to_json_skips_absent_tables():
    """Tables missing in a pre-migration DB are silently skipped (except branch)."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE mistakes (id INTEGER PRIMARY KEY, mistake TEXT)")
    conn.execute("INSERT INTO mistakes (mistake) VALUES ('boom')")

    data = export_to_json(conn)

    assert data["mistakes"] == [{"id": 1, "mistake": "boom"}]
    # patterns/skills/... tables do not exist → keys absent, no exception raised
    assert "patterns" not in data
    assert "reflexes" not in data


def test_run_backup_writes_json_file(test_db, capsys):
    """run_backup dumps every core table to a timestamped JSON file."""
    conn = test_db["conn"]
    conn.execute(
        "INSERT INTO skills (name, domain, trigger_desc, workflow) VALUES ('s', 'd', 't', 'w')"
    )
    conn.commit()

    filepath = run_backup(git_sync=False)

    assert filepath.endswith(".json")
    with open(filepath) as f:
        payload = json.load(f)
    assert len(payload["skills"]) == 1
    assert payload["skills"][0]["name"] == "s"
    assert "mistakes" in payload  # empty table still present
    out = capsys.readouterr().out
    assert "exported successfully" in out
    assert filepath in out


def test_run_backup_git_sync_success(test_db, capsys):
    """git_sync path: status/add/commit/push all succeed."""
    with patch("src.backup.subprocess.run") as run:
        run.return_value = subprocess.CompletedProcess([], 0)
        run_backup(git_sync=True)

    out = capsys.readouterr().out
    assert "committed to Git" in out
    assert "pushed to remote" in out
    # git status, add, commit, push
    assert run.call_count == 4


def test_run_backup_git_push_fails_commit_kept(test_db, capsys):
    """A failing push still keeps the local commit and prints the note."""

    def side_effect(cmd, **kwargs):
        if cmd[:2] == ["git", "push"]:
            raise subprocess.CalledProcessError(1, cmd)
        return subprocess.CompletedProcess([], 0)

    with patch("src.backup.subprocess.run", side_effect=side_effect):
        run_backup(git_sync=True)

    out = capsys.readouterr().out
    assert "committed to Git" in out
    assert "No remote configured or push failed" in out


def test_run_backup_git_not_a_repo(test_db, capsys):
    """git status failing means the dir is not a repo → sync skipped cleanly."""
    with patch(
        "src.backup.subprocess.run",
        side_effect=subprocess.CalledProcessError(128, ["git", "status"]),
    ):
        run_backup(git_sync=True)

    out = capsys.readouterr().out
    assert "not a git repository" in out
    assert "committed to Git" not in out


# ──────────────────────────── migrations.py ────────────────────────────


def test_normalize_vec_memory_rescales_to_unit_length():
    """Non-unit vectors (json + bytes) are rescaled; unit/zero left untouched."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE vec_memory (rowid INTEGER PRIMARY KEY, embedding)")
    conn.execute("INSERT INTO vec_memory VALUES (1, ?)", (json.dumps([3.0, 4.0]),))
    conn.execute("INSERT INTO vec_memory VALUES (2, ?)", (struct.pack("2f", 6.0, 8.0),))
    conn.execute("INSERT INTO vec_memory VALUES (3, ?)", (json.dumps([1.0, 0.0]),))
    conn.execute("INSERT INTO vec_memory VALUES (4, ?)", (json.dumps([0.0, 0.0]),))

    _normalize_vec_memory(conn)

    v1 = json.loads(conn.execute("SELECT embedding FROM vec_memory WHERE rowid=1").fetchone()[0])
    assert v1 == pytest.approx([0.6, 0.8])
    v2 = json.loads(conn.execute("SELECT embedding FROM vec_memory WHERE rowid=2").fetchone()[0])
    assert math.isclose(math.sqrt(sum(x * x for x in v2)), 1.0, abs_tol=1e-6)
    # already-unit vector left as-is (still valid json list)
    v3 = json.loads(conn.execute("SELECT embedding FROM vec_memory WHERE rowid=3").fetchone()[0])
    assert v3 == [1.0, 0.0]


def test_normalize_vec_memory_no_table_is_noop():
    """Missing vec_memory (extension unavailable) is swallowed, not raised."""
    conn = sqlite3.connect(":memory:")
    _normalize_vec_memory(conn)  # must not raise


def test_add_column_if_missing_is_idempotent():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (id INTEGER)")
    _add_column_if_missing(conn, "t", "flag", "INTEGER DEFAULT 0")
    cols = {r[1] for r in conn.execute("PRAGMA table_info(t)").fetchall()}
    assert "flag" in cols
    # second call is a no-op (no duplicate-column error)
    _add_column_if_missing(conn, "t", "flag", "INTEGER DEFAULT 0")
    cols2 = [r[1] for r in conn.execute("PRAGMA table_info(t)").fetchall()]
    assert cols2.count("flag") == 1


def test_swap_fts_table_no_fts_is_noop():
    """No memory_fts table → PRAGMA yields no cols → early return, no error."""
    conn = sqlite3.connect(":memory:")
    _swap_fts_table(conn, "", "staging")  # must not raise


def test_rebuild_fts_with_porter_preserves_rows_and_triggers(test_db):
    """Rebuilding memory_fts keeps rows and recreates dependent triggers."""
    conn = test_db["conn"]
    _seed_fts(conn, "skill", 1, "Porter Rebuild", "committing verification changes")
    conn.commit()
    triggers_before = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' AND sql LIKE '%memory_fts%'"
    ).fetchone()[0]

    _rebuild_fts_with_porter(conn)

    row = conn.execute(
        "SELECT title FROM memory_fts WHERE memory_fts MATCH '\"commit\"'"
    ).fetchone()
    assert row["title"] == "Porter Rebuild"
    triggers_after = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' AND sql LIKE '%memory_fts%'"
    ).fetchone()[0]
    assert triggers_after == triggers_before


def test_run_migrations_skips_gap_and_bumps_version(test_db):
    """v1 has no migration entry (skipped); v2 creates its marker table."""
    conn = test_db["conn"]
    ok = run_migrations(conn, 0, 2)
    assert ok is True
    exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE name='_test_migration_v2'"
    ).fetchone()
    assert exists is not None
    ver = conn.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone()["value"]
    assert ver == "2"


def test_run_migrations_v6_rebuilds_fts(test_db):
    """v6 triggers a full FTS rebuild that re-indexes core tables from scratch."""
    conn = test_db["conn"]
    conn.execute(
        "INSERT INTO skills (name, domain, trigger_desc, workflow) "
        "VALUES ('Rebuilt Skill', 'd', 'trigger text', 'workflow text')"
    )
    conn.commit()
    with patch("src.database.embed_text", return_value=[0.1] * 768):
        ok = run_migrations(conn, 5, 6)
    assert ok is True
    ver = conn.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone()["value"]
    assert ver == "6"
    # rebuild_fts re-indexed the skill row from the source table
    row = conn.execute(
        "SELECT title FROM memory_fts WHERE item_type='skill'"
    ).fetchone()
    assert row["title"] == "Rebuilt Skill"


def test_downgrade_noop_when_target_not_below_current(test_db):
    assert downgrade_to(test_db["conn"], 5, 10) is True


def test_downgrade_drops_tables_and_updates_version(test_db):
    """Downgrading 19→15 drops skill_tests (v19) and inbox (v17)."""
    conn = test_db["conn"]
    assert conn.execute(
        "SELECT name FROM sqlite_master WHERE name='skill_tests'"
    ).fetchone() is not None

    downgrade_to(conn, 19, 15)

    assert conn.execute(
        "SELECT name FROM sqlite_master WHERE name='skill_tests'"
    ).fetchone() is None
    assert conn.execute(
        "SELECT name FROM sqlite_master WHERE name='inbox'"
    ).fetchone() is None
    ver = conn.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone()["value"]
    assert ver == "15"


def test_downgrade_skips_versions_without_scripts(test_db):
    """v6/v5 have no DOWNGRADES entry → the skip branch leaves version untouched."""
    conn = test_db["conn"]
    before = conn.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone()["value"]
    assert downgrade_to(conn, 6, 4) is True
    # both v6 and v5 hit the 'no downgrade script' continue, so schema_meta is never bumped
    after = conn.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone()["value"]
    assert after == before


def test_downgrade_swallows_failing_step(test_db):
    """A failing downgrade step is caught and logged, not raised."""
    conn = test_db["conn"]
    with patch.dict(
        "src.migrations.DOWNGRADES",
        {20: ["DROP TABLE table_that_does_not_exist"]},
        clear=False,
    ):
        # should not raise despite the bad SQL
        assert downgrade_to(conn, 20, 19) is True
    ver = conn.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone()["value"]
    assert ver == "19"


def test_backup_before_migration_copies_existing_db(test_db):
    path = backup_before_migration(test_db["path"], 7)
    assert path is not None
    assert path.endswith("pre-migration-v7.db")
    import os

    assert os.path.exists(path)


def test_backup_before_migration_none_when_missing():
    assert backup_before_migration("/nonexistent/path/to/engram.db", 3) is None


# ────────────────────────────── search.py ──────────────────────────────


def test_fts_query_terms_tokenizes_and_handles_edge_cases():
    assert _fts_query_terms("migration.") == ["migration"]
    assert _fts_query_terms("Foo Bar-Baz") == ["foo", "bar", "baz"]
    assert _fts_query_terms("   ") == []
    # no ascii alnum but contains alpha (CJK) → whitespace split fallback
    assert _fts_query_terms("日本 語") == ["日本", "語"]
    # no ascii alnum and no alpha → final split fallback
    assert _fts_query_terms("!!! ???") == ["!!!", "???"]


def test_fts5_tag_phrase_quotes_and_escapes():
    assert _fts5_tag_phrase("ai-assistant") == '"ai-assistant"'
    assert _fts5_tag_phrase("") == ""
    assert _fts5_tag_phrase('a"b') == '"a""b"'


def test_get_stale_rowids_returns_marked_rows(test_db):
    conn = test_db["conn"]
    rid = _seed_fts(conn, "skill", 1, "t", "c")
    conn.execute(
        "INSERT INTO embedding_status (fts_rowid, item_type, item_id, status) "
        "VALUES (?, 'skill', 1, 'stale')",
        (rid,),
    )
    conn.commit()
    assert _get_stale_rowids(conn) == {rid}


def test_get_stale_rowids_missing_table_returns_empty():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    assert _get_stale_rowids(conn) == set()


def test_semantic_search_unavailable_when_host_down():
    with patch("src.search.embed_text", return_value=None), \
         patch("src.search.is_embedding_host_available", return_value=False):
        results, status = semantic_search("anything")
    assert results == []
    assert status == "unavailable"


def test_semantic_search_unavailable_with_degradation_reason():
    with patch("src.search.embed_text", return_value=None), \
         patch("src.search.is_embedding_host_available", return_value=True), \
         patch("src.search.get_embedding_degradation_reason", return_value="model missing"):
        results, status = semantic_search("anything")
    assert results == []
    assert status == "unavailable"


def test_semantic_search_returns_knn_hits(test_db):
    """embed_text vector + seeded vec_memory returns ranked semantic hits."""
    conn = test_db["conn"]
    rid = _seed_fts(conn, "skill", 1, "Vec Hit", "semantic content", "python")
    conn.execute(
        "INSERT INTO vec_memory(rowid, embedding) VALUES (?, ?)",
        (rid, json.dumps([0.1] * 768)),
    )
    conn.commit()

    with patch("src.search.embed_text", return_value=[0.1] * 768):
        results, status = semantic_search(
            "query", item_type="skill", tags=["python"], db_path=test_db["path"]
        )

    assert status == "ok"
    assert len(results) == 1
    hit = results[0]
    assert hit["title"] == "Vec Hit"
    assert hit["is_semantic"] is True
    assert hit["item_type"] == "skill"
    assert hit["rowid"] == rid


def test_search_lexical_with_query_and_tag_filter(test_db):
    """Lexical FTS path: query terms match, explicit tag filters the results."""
    conn = test_db["conn"]
    conn.execute(
        "INSERT INTO mistakes (date, context, mistake, fix) VALUES "
        "('2026-01-01', 'ctx', 'flaky pipeline crash', 'retry')"
    )
    _seed_fts(conn, "mistake", 1, "flaky pipeline crash", "ctx | flaky pipeline crash", "ci")
    _seed_fts(conn, "skill", 2, "unrelated skill", "pipeline helper", "docs")
    conn.commit()

    with patch("src.search.embed_text", return_value=[]), \
         patch("src.search.is_embedding_host_available", return_value=False):
        results = search("pipeline", tags=["ci"], db_path=test_db["path"])

    titles = [r["title"] for r in results]
    assert "flaky pipeline crash" in titles
    assert "unrelated skill" not in titles  # filtered out by tag 'ci'
    assert results.semantic_status == "unavailable"


def test_search_empty_query_lists_recent(test_db):
    """Empty query uses the no-MATCH branch ordered by rowid DESC."""
    conn = test_db["conn"]
    _seed_fts(conn, "skill", 1, "first", "a", "x")
    _seed_fts(conn, "skill", 2, "second", "b", "x")
    conn.commit()

    with patch("src.search.embed_text", return_value=[]), \
         patch("src.search.is_embedding_host_available", return_value=False):
        results = search("", db_path=test_db["path"])

    titles = [r["title"] for r in results]
    assert titles[0] == "second"  # most recent rowid first
    assert "first" in titles


def test_search_prepends_pinned_and_hides_superseded(test_db):
    """Pinned items lead; [SUPERSEDED]-titled items are hidden by default."""
    from src.database import pin_item

    conn = test_db["conn"]
    conn.execute(
        "INSERT INTO skills (name, domain, trigger_desc, workflow) VALUES ('Pinned One', 'd', 'pipeline', 'w')"
    )
    conn.execute(
        "INSERT INTO skills (name, domain, trigger_desc, workflow) VALUES ('Old Skill', 'd', 'pipeline', 'w')"
    )
    _seed_fts(conn, "skill", 1, "Pinned One", "pipeline stuff")
    _seed_fts(conn, "skill", 2, "[SUPERSEDED] Old Skill", "pipeline stuff old")
    conn.commit()
    pin_item("skill", 1, db_path=test_db["path"])

    with patch("src.search.embed_text", return_value=[]), \
         patch("src.search.is_embedding_host_available", return_value=False):
        results = search("pipeline", db_path=test_db["path"])

    titles = [r["title"] for r in results]
    assert titles[0] == "Pinned One"  # pinned prepended
    assert not any(t.startswith("[SUPERSEDED]") for t in titles)

    # include_superseded=True brings the hidden item back
    with patch("src.search.embed_text", return_value=[]), \
         patch("src.search.is_embedding_host_available", return_value=False):
        results2 = search("pipeline", db_path=test_db["path"], include_superseded=True)
    assert any(t.startswith("[SUPERSEDED]") for t in [r["title"] for r in results2])


def test_search_with_project_path_computes_affinity(test_db, tmp_path):
    """project_path triggers get_or_create_project + affinity lookup path."""
    conn = test_db["conn"]
    conn.execute(
        "INSERT INTO skills (name, domain, trigger_desc, workflow) VALUES ('Proj Skill', 'd', 'deploy', 'w')"
    )
    _seed_fts(conn, "skill", 1, "Proj Skill", "deploy pipeline")
    conn.commit()

    proj_dir = str(tmp_path / "myproj")
    with patch("src.search.embed_text", return_value=[]), \
         patch("src.search.is_embedding_host_available", return_value=False):
        results = search("deploy", project_path=proj_dir, db_path=test_db["path"])

    assert [r["title"] for r in results] == ["Proj Skill"]
    # the project was created as a side effect
    row = conn.execute("SELECT name FROM projects WHERE path = ?", (proj_dir,)).fetchone()
    assert row is not None


def test_get_recent_returns_latest_filtered_by_type(test_db):
    conn = test_db["conn"]
    _seed_fts(conn, "skill", 1, "skill a", "x", "t1")
    _seed_fts(conn, "mistake", 2, "mistake b", "y", "t2")
    _seed_fts(conn, "skill", 3, "skill c", "z", "t3")
    conn.commit()

    recent = get_recent(limit=10, item_type="skill", db_path=test_db["path"])
    titles = [r["title"] for r in recent]
    assert titles == ["skill c", "skill a"]  # rowid DESC, mistake excluded
    assert all(r["item_type"] == "skill" for r in recent)
    assert recent[0]["tags"] == "t3"


def test_get_stats_counts_tables_and_fts(test_db):
    conn = test_db["conn"]
    conn.execute(
        "INSERT INTO skills (name, domain, trigger_desc, workflow) VALUES ('s', 'd', 't', 'w')"
    )
    conn.execute(
        "INSERT INTO patterns (name, symptoms, root_cause, standard_fix) VALUES ('p', 's', 'r', 'f')"
    )
    _seed_fts(conn, "skill", 1, "s", "t w")
    conn.commit()

    stats = get_stats(db_path=test_db["path"])
    assert stats["skills"] == 1
    assert stats["patterns"] == 1
    assert stats["mistakes"] == 0
    assert stats["fts_indexed"] >= 1
    assert "embeddings" in stats
