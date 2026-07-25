"""Best-effort importers from competing memory exports (Mem0 / Zep / OpenMemory)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .database import get_connection
from .memory_ops import create_conversation, create_mistake, create_pattern, create_skill


def _load(path: str | Path) -> Any:
    data = Path(path).read_text(encoding="utf-8")
    return json.loads(data)


def _as_list(payload: Any) -> list[dict]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        for key in ("memories", "results", "items", "nodes", "episodes"):
            if isinstance(payload.get(key), list):
                return [x for x in payload[key] if isinstance(x, dict)]
        return [payload]
    return []


def import_mem0_export(path: str | Path, *, db_path=None, tags: list[str] | None = None) -> dict:
    """Import a Mem0-style JSON export (list of {memory,text,data,...})."""
    rows = _as_list(_load(path))
    added = skipped = 0
    tag_list = list(tags or ["imported", "mem0"])
    with get_connection(db_path) as conn:
        for row in rows:
            text = (
                row.get("memory")
                or row.get("text")
                or row.get("data")
                or row.get("content")
                or ""
            )
            if isinstance(text, dict):
                text = json.dumps(text, ensure_ascii=False)
            text = str(text).strip()
            if not text:
                skipped += 1
                continue
            title = (row.get("metadata") or {}).get("title") if isinstance(row.get("metadata"), dict) else None
            name = (title or text.split(".")[0])[:80] or "imported-memory"
            try:
                create_skill(
                        conn,
                        name=f"mem0:{name}"[:80],
                        domain="imported",
                        trigger=text[:200],
                        workflow=text,
                        pitfalls=None,
                        tags=tag_list,
                    )
                added += 1
            except Exception:
                skipped += 1
    return {"source": "mem0", "added": added, "skipped": skipped, "seen": len(rows)}


def import_zep_export(path: str | Path, *, db_path=None, tags: list[str] | None = None) -> dict:
    """Import Graphiti/Zep JSON (edges/facts or episode summaries)."""
    payload = _load(path)
    rows = _as_list(payload)
    added = skipped = 0
    tag_list = list(tags or ["imported", "zep"])
    with get_connection(db_path) as conn:
        for row in rows:
            fact = (
                row.get("fact")
                or row.get("name")
                or row.get("content")
                or row.get("summary")
                or ""
            )
            fact = str(fact).strip()
            if not fact:
                # triple-shaped
                subj = row.get("source_node_uuid") or row.get("subject") or ""
                pred = row.get("name") or row.get("predicate") or "related"
                obj = row.get("target_node_uuid") or row.get("object") or ""
                if subj or obj:
                    fact = f"{subj} {pred} {obj}".strip()
            if not fact:
                skipped += 1
                continue
            try:
                create_pattern(
                    conn,
                    name=f"zep:{fact}"[:80],
                    symptoms=fact,
                    root_cause=str(row.get("valid_at") or row.get("created_at") or "imported"),
                    standard_fix=str(row.get("invalid_at") or "See timeline / invalidate when stale."),
                    tags=tag_list,
                )
                added += 1
            except Exception:
                skipped += 1
    return {"source": "zep", "added": added, "skipped": skipped, "seen": len(rows)}


def import_openmemory_export(path: str | Path, *, db_path=None, tags: list[str] | None = None) -> dict:
    """Import OpenMemory-style sector memories."""
    rows = _as_list(_load(path))
    added = skipped = 0
    tag_list = list(tags or ["imported", "openmemory"])
    with get_connection(db_path) as conn:
        for row in rows:
            sector = (row.get("sector") or row.get("type") or "semantic").lower()
            text = str(row.get("content") or row.get("text") or row.get("memory") or "").strip()
            if not text:
                skipped += 1
                continue
            try:
                if sector in ("episodic", "experience", "conversation"):
                    create_conversation(
                        conn,
                        conversation_id=str(row.get("id") or row.get("uuid") or f"om-{added}")[:64],
                        title=(row.get("title") or text[:60])[:80],
                        date=str(row.get("date") or row.get("created_at") or "")[:32] or __import__("datetime").date.today().isoformat(),
                        domain="imported",
                        tasks_completed=text,
                        key_decisions=None,
                        tags=tag_list,
                    )
                elif sector in ("procedural", "skill"):
                    create_skill(
                        conn,
                        name=f"om:{(row.get('title') or text[:40])}"[:80],
                        domain="imported",
                        trigger=text[:200],
                        workflow=text,
                        pitfalls=None,
                        tags=tag_list,
                    )
                else:
                    create_mistake(
                        conn,
                        date=__import__("datetime").date.today().isoformat(),
                        context="openmemory-import",
                        mistake=text[:200],
                        fix=str(row.get("resolution") or "Imported fact — verify before acting."),
                        root_cause=sector,
                        prevention=None,
                        tags=tag_list,
                    )
                added += 1
            except Exception:
                skipped += 1
    return {"source": "openmemory", "added": added, "skipped": skipped, "seen": len(rows)}
