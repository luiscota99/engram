"""Chunk sync: export → import round-trip across two databases, idempotently."""
import gzip
import json
import os

import pytest

from src.chunk_sync import export_chunks, import_chunks, sync_status
from src.database import get_connection, init_db
from src.memory_ops import (
    create_conversation,
    create_mistake,
    create_pattern,
    create_prompt,
    create_skill,
)


@pytest.fixture
def two_dbs(tmp_path):
    """Two independent DBs (machine A and machine B) plus a shared sync dir."""
    paths = {}
    for name in ("a", "b"):
        p = str(tmp_path / f"{name}.db")
        init_db(p)
        paths[name] = p
    os.environ["ENGRAM_DB_PATH"] = paths["a"]
    yield {"a": paths["a"], "b": paths["b"], "sync": tmp_path / "shared"}


def _seed(conn):
    create_mistake(
        conn, date="2026-07-17", context="ctx", mistake="broke it", fix="fixed it",
        root_cause="rc", prevention="prev", tags="alpha,beta",
    )
    create_pattern(conn, name="P1", symptoms="sym", root_cause="rc", standard_fix="sf", tags="alpha")
    create_skill(conn, name="S1", domain="dev", trigger="when x", workflow="1. do x", pitfalls="careful")
    create_conversation(conn, conversation_id="c-1", title="Conv", date="2026-07-17", domain="dev")
    create_prompt(conn, name="Pr1", role="system", domain="dev", description="desc", prompt_text="text")
    conn.commit()


def test_round_trip_and_idempotency(two_dbs):
    sync_dir = two_dbs["sync"]

    with get_connection(two_dbs["a"]) as conn_a:
        _seed(conn_a)
        result = export_chunks(conn_a, sync_dir)
        assert result["exported"] == 5
        assert result["chunk"].endswith(".jsonl.gz")

        # re-export with nothing new is a no-op (no second chunk)
        again = export_chunks(conn_a, sync_dir)
        assert again["exported"] == 0

    chunks = list((sync_dir / "chunks").glob("*.jsonl.gz"))
    assert len(chunks) == 1

    with get_connection(two_dbs["b"]) as conn_b:
        result = import_chunks(conn_b, sync_dir)
        assert result["imported"] == 5
        assert result["by_type"] == {
            "mistake": 1, "pattern": 1, "skill": 1, "conversation": 1, "prompt": 1,
        }

        # imported rows landed with fields, tags and FTS intact
        row = conn_b.execute("SELECT context, mistake, fix FROM mistakes").fetchone()
        assert tuple(row) == ("ctx", "broke it", "fixed it")
        tags = {
            r[0] for r in conn_b.execute(
                "SELECT t.name FROM item_tags it JOIN tags t ON t.id = it.tag_id WHERE it.item_type='mistake'"
            )
        }
        assert tags == {"alpha", "beta"}
        fts_n = conn_b.execute("SELECT count(*) FROM memory_fts WHERE memory_fts MATCH 'broke'").fetchone()[0]
        assert fts_n >= 1

        # importing twice changes nothing
        second = import_chunks(conn_b, sync_dir)
        assert second["imported"] == 0

        # B has nothing new to offer back to the shared dir
        assert export_chunks(conn_b, sync_dir)["exported"] == 0

        status = sync_status(conn_b, sync_dir)
        assert status["to_import"] == 0
        assert status["to_export"] == 0
        assert status["remote_entries"] == 5


def test_divergent_machines_converge(two_dbs):
    sync_dir = two_dbs["sync"]
    with get_connection(two_dbs["a"]) as conn_a:
        create_mistake(conn_a, date="2026-07-17", context="only-a", mistake="ma", fix="fa")
        conn_a.commit()
        export_chunks(conn_a, sync_dir)
    with get_connection(two_dbs["b"]) as conn_b:
        create_mistake(conn_b, date="2026-07-17", context="only-b", mistake="mb", fix="fb")
        conn_b.commit()
        import_chunks(conn_b, sync_dir)   # B pulls A's entry
        export_chunks(conn_b, sync_dir)   # B pushes its own
        assert conn_b.execute("SELECT count(*) FROM mistakes").fetchone()[0] == 2
    with get_connection(two_dbs["a"]) as conn_a:
        import_chunks(conn_a, sync_dir)   # A pulls B's entry
        assert conn_a.execute("SELECT count(*) FROM mistakes").fetchone()[0] == 2
    # two chunks, none ever rewritten
    assert len(list((sync_dir / "chunks").glob("*.jsonl.gz"))) == 2


def test_corrupt_lines_never_block_import(two_dbs):
    sync_dir = two_dbs["sync"]
    chunks_dir = sync_dir / "chunks"
    chunks_dir.mkdir(parents=True)
    good = {"k": "deadbeefdeadbeef", "t": "pattern",
            "f": {"name": "OK", "symptoms": "s", "root_cause": "r", "standard_fix": "f"}, "tags": []}
    with gzip.open(chunks_dir / "x.jsonl.gz", "wt", encoding="utf-8") as fh:
        fh.write("not json at all\n")
        fh.write(json.dumps({"k": "aaaabbbbccccdddd", "t": "pattern", "f": {"name": "broken"}}) + "\n")
        fh.write(json.dumps(good) + "\n")
    with get_connection(two_dbs["a"]) as conn:
        result = import_chunks(conn, sync_dir)
        assert result["imported"] == 1          # the good record
        assert result["skipped"] >= 1           # the malformed one
        assert conn.execute("SELECT count(*) FROM patterns").fetchone()[0] == 1
