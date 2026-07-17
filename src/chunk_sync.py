"""Git-native chunk sync: share memories across machines/teams via a plain directory.

Entries export as append-only gzipped-JSONL chunk files named by content
hash. Old chunks never mutate and filenames never collide, so a git repo (or
any synced folder) holding the chunk dir merges without conflicts by
construction — no manifest, no server. The local SQLite DB remains the
source of truth.

Import replays foreign entries through the canonical ``memory_ops.create_*``
paths, so FTS indexing, tag linking and embedding queueing behave exactly like
a local add. Identity is a content hash over each type's dedup fields (the
same fields write-time dedup uses), making export and import idempotent.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import socket
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from .memory_ops import (
    create_conversation,
    create_mistake,
    create_pattern,
    create_prompt,
    create_skill,
    mistake_dedup_content,
    pattern_dedup_content,
    skill_dedup_content,
)

DEFAULT_SYNC_DIR = "~/.engram/sync"
CHUNKS_SUBDIR = "chunks"


@dataclass(frozen=True)
class TypeSpec:
    table: str
    columns: tuple[str, ...]
    identity: Callable[[dict], str]
    create: Callable[[Any, dict, list[str]], int]


TYPES: dict[str, TypeSpec] = {
    "mistake": TypeSpec(
        table="mistakes",
        columns=("date", "context", "mistake", "root_cause", "fix", "prevention", "conversation_id"),
        identity=lambda f: mistake_dedup_content(
            f["context"], f["mistake"], f.get("root_cause"), f["fix"], f.get("prevention")
        ),
        create=lambda conn, f, tags: create_mistake(
            conn,
            date=f["date"],
            context=f["context"],
            mistake=f["mistake"],
            fix=f["fix"],
            root_cause=f.get("root_cause"),
            prevention=f.get("prevention"),
            conversation_id=f.get("conversation_id"),
            tags=tags,
        ),
    ),
    "pattern": TypeSpec(
        table="patterns",
        columns=("name", "symptoms", "root_cause", "standard_fix"),
        identity=lambda f: pattern_dedup_content(f["symptoms"], f["root_cause"], f["standard_fix"]),
        create=lambda conn, f, tags: create_pattern(
            conn,
            name=f["name"],
            symptoms=f["symptoms"],
            root_cause=f["root_cause"],
            standard_fix=f["standard_fix"],
            tags=tags,
        ),
    ),
    "skill": TypeSpec(
        table="skills",
        columns=("name", "domain", "trigger_desc", "workflow", "pitfalls", "key_files", "dependencies"),
        identity=lambda f: skill_dedup_content(f["trigger_desc"], f["workflow"], f.get("pitfalls")),
        create=lambda conn, f, tags: create_skill(
            conn,
            name=f["name"],
            domain=f["domain"],
            trigger=f["trigger_desc"],
            workflow=f["workflow"],
            pitfalls=f.get("pitfalls"),
            key_files=f.get("key_files"),
            dependencies=f.get("dependencies"),
            tags=tags,
        ),
    ),
    "conversation": TypeSpec(
        table="conversations",
        columns=(
            "conversation_id", "title", "date", "domain",
            "tasks_completed", "key_decisions", "mistakes_summary", "skills_extracted",
        ),
        identity=lambda f: f"{f.get('conversation_id') or ''}|{f['title']}",
        create=lambda conn, f, tags: create_conversation(
            conn,
            conversation_id=f.get("conversation_id") or f"sync-{_sync_key('conversation', f['title'])}",
            title=f["title"],
            date=f["date"],
            domain=f["domain"],
            tasks_completed=f.get("tasks_completed"),
            key_decisions=f.get("key_decisions"),
            mistakes_summary=f.get("mistakes_summary"),
            skills_extracted=f.get("skills_extracted"),
            tags=tags,
        ),
    ),
    "prompt": TypeSpec(
        table="prompts",
        columns=("name", "role", "domain", "description", "prompt_text", "source_path", "best_for"),
        identity=lambda f: f"{f['name']}|{f['role']}",
        create=lambda conn, f, tags: create_prompt(
            conn,
            name=f["name"],
            role=f["role"],
            domain=f["domain"],
            description=f["description"],
            prompt_text=f.get("prompt_text"),
            source_path=f.get("source_path"),
            best_for=f.get("best_for"),
            tags=tags,
        ),
    ),
}


def _sync_key(item_type: str, identity: str) -> str:
    return hashlib.sha256(f"{item_type}\n{identity}".encode()).hexdigest()[:16]


def _tags_by_item(conn, item_type: str) -> dict[int, list[str]]:
    """All tags for a type in one query (no per-row lookups)."""
    rows = conn.execute(
        """SELECT it.item_id, t.name FROM item_tags it
           JOIN tags t ON t.id = it.tag_id WHERE it.item_type = ? ORDER BY t.name""",
        (item_type,),
    ).fetchall()
    out: dict[int, list[str]] = {}
    for item_id, name in rows:
        out.setdefault(item_id, []).append(name)
    return out


def _local_records(conn) -> Iterable[dict]:
    """Yield every syncable local entry as a portable record."""
    for item_type, spec in TYPES.items():
        tags = _tags_by_item(conn, item_type)
        cols = ", ".join(spec.columns)
        for row in conn.execute(f"SELECT id, {cols} FROM {spec.table}").fetchall():  # noqa: S608 — table/cols from static registry
            fields = {c: row[i + 1] for i, c in enumerate(spec.columns)}
            yield {
                "k": _sync_key(item_type, spec.identity(fields)),
                "t": item_type,
                "f": fields,
                "tags": tags.get(row[0], []),
            }


def _chunk_files(sync_dir: Path) -> list[Path]:
    chunks = sync_dir / CHUNKS_SUBDIR
    return sorted(chunks.glob("*.jsonl.gz")) if chunks.is_dir() else []


def _read_chunks(sync_dir: Path) -> dict[str, dict]:
    """All remote records keyed by sync key. Later chunks win on (unexpected) key repeats."""
    records: dict[str, dict] = {}
    for path in _chunk_files(sync_dir):
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue  # a corrupt line never blocks the rest of the chunk
                if isinstance(rec, dict) and rec.get("k") and rec.get("t") in TYPES:
                    records[rec["k"]] = rec
    return records


def export_chunks(conn, sync_dir: str | Path | None = None) -> dict:
    """Write local entries not yet present in the sync dir as one new chunk."""
    sync_dir = Path(os.path.expanduser(str(sync_dir or DEFAULT_SYNC_DIR)))
    remote_keys = set(_read_chunks(sync_dir))
    new = [r for r in _local_records(conn) if r["k"] not in remote_keys]
    result = {"dir": str(sync_dir), "exported": len(new), "chunk": None}
    if not new:
        return result

    new.sort(key=lambda r: r["k"])
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    machine = socket.gethostname()
    payload = "".join(
        json.dumps({**r, "exported_at": stamp, "machine": machine}, ensure_ascii=False) + "\n"
        for r in new
    )
    digest = hashlib.sha256(payload.encode()).hexdigest()[:12]
    chunks_dir = sync_dir / CHUNKS_SUBDIR
    chunks_dir.mkdir(parents=True, exist_ok=True)
    chunk_path = chunks_dir / f"{stamp}-{digest}.jsonl.gz"
    with gzip.open(chunk_path, "wt", encoding="utf-8") as fh:
        fh.write(payload)
    result["chunk"] = chunk_path.name
    return result


def import_chunks(conn, sync_dir: str | Path | None = None) -> dict:
    """Create every remote entry the local DB doesn't have. Idempotent."""
    sync_dir = Path(os.path.expanduser(str(sync_dir or DEFAULT_SYNC_DIR)))
    local_keys = {r["k"] for r in _local_records(conn)}
    imported: dict[str, int] = {}
    skipped = 0
    for key, rec in sorted(_read_chunks(sync_dir).items()):
        if key in local_keys:
            skipped += 1
            continue
        spec = TYPES[rec["t"]]
        fields = rec.get("f") or {}
        try:
            spec.create(conn, fields, list(rec.get("tags") or []))
        except (KeyError, TypeError):
            skipped += 1  # malformed foreign record — skip, never abort the batch
            continue
        local_keys.add(key)
        imported[rec["t"]] = imported.get(rec["t"], 0) + 1
    conn.commit()
    total = sum(imported.values())
    return {"dir": str(sync_dir), "imported": total, "by_type": imported, "skipped": skipped}


def sync_status(conn, sync_dir: str | Path | None = None) -> dict:
    """How local and remote compare, without changing anything."""
    sync_dir = Path(os.path.expanduser(str(sync_dir or DEFAULT_SYNC_DIR)))
    local = {r["k"] for r in _local_records(conn)}
    remote = set(_read_chunks(sync_dir))
    return {
        "dir": str(sync_dir),
        "chunks": len(_chunk_files(sync_dir)),
        "local_entries": len(local),
        "remote_entries": len(remote),
        "to_export": len(local - remote),
        "to_import": len(remote - local),
    }
