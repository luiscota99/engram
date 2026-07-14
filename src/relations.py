"""Typed relationships between memory items.

The pragmatic, LLM-era slice of a knowledge graph: a small **closed** vocabulary
of edge types over the memories Engram already stores — no OWL/RDF, no reasoner,
no SPARQL. The LLM is the reasoner; this just gives it explicit, cheap-to-traverse
links so recall can surface connected prior art ("this mistake is *caused by* that
pattern"; "this skill *supersedes* that one").

Kept low-friction on purpose (the lesson of a memory nobody reuses): the
vocabulary is tiny, links are one call to create, and some are recorded
automatically (a merge writes ``supersedes``).
"""

from __future__ import annotations

from .database import get_connection

# Closed vocabulary. Directional: `from` --relation--> `to`.
RELATION_TYPES = {
    "supersedes": "`from` replaces/obsoletes `to`",
    "refines": "`from` is a more precise version of `to`",
    "causes": "`from` leads to / triggers `to`",
    "contradicts": "`from` conflicts with `to` (resolve which is right)",
    "depends_on": "`from` requires `to` to hold",
    "related": "`from` and `to` are relevant to each other (generic)",
}

_ITEM_TABLES = {
    "mistake": "mistakes",
    "pattern": "patterns",
    "skill": "skills",
    "conversation": "conversations",
    "prompt": "prompts",
    "session": "sessions",
}


def _title_for(conn, item_type: str, item_id: int) -> str | None:
    """Best-effort human label for an item (None if it doesn't exist)."""
    table = _ITEM_TABLES.get(item_type)
    if not table:
        return None
    col = {
        "mistakes": "mistake",
        "patterns": "name",
        "skills": "name",
        "conversations": "title",
        "prompts": "name",
        "sessions": "title",
    }[table]
    row = conn.execute(f"SELECT {col} AS label FROM {table} WHERE id = ?", (item_id,)).fetchone()
    return row["label"] if row else None


def add_relation(
    from_type: str,
    from_id: int,
    to_type: str,
    to_id: int,
    relation: str,
    *,
    source: str = "manual",
    db_path=None,
    validate_exists: bool = True,
) -> str | None:
    """Create a typed edge. Returns an error string, or None on success.

    Idempotent (the UNIQUE constraint means re-linking is a no-op). ``source``
    tags manual vs auto-derived edges. With ``validate_exists`` both endpoints
    must be real items.
    """
    if relation not in RELATION_TYPES:
        return f"Unknown relation '{relation}'. Choose from: {', '.join(sorted(RELATION_TYPES))}."
    if from_type not in _ITEM_TABLES or to_type not in _ITEM_TABLES:
        return f"Item types must be one of: {', '.join(sorted(_ITEM_TABLES))}."
    if (from_type, from_id) == (to_type, to_id):
        return "An item cannot relate to itself."

    with get_connection(db_path) as conn:
        if validate_exists:
            if _title_for(conn, from_type, from_id) is None:
                return f"No {from_type} with id {from_id}."
            if _title_for(conn, to_type, to_id) is None:
                return f"No {to_type} with id {to_id}."
        conn.execute(
            "INSERT OR IGNORE INTO memory_relations "
            "(from_type, from_id, to_type, to_id, relation, source) VALUES (?, ?, ?, ?, ?, ?)",
            (from_type, from_id, to_type, to_id, relation, source),
        )
    return None


def get_relations(item_type: str, item_id: int, db_path=None) -> list[dict]:
    """All edges touching an item, each with the other endpoint's title.

    Returns dicts: ``{direction: 'out'|'in', relation, other_type, other_id,
    other_title, source}``. Outgoing first, then incoming.
    """
    out: list[dict] = []
    with get_connection(db_path) as conn:
        for row in conn.execute(
            "SELECT to_type, to_id, relation, source FROM memory_relations "
            "WHERE from_type = ? AND from_id = ? ORDER BY relation",
            (item_type, item_id),
        ).fetchall():
            out.append({
                "direction": "out",
                "relation": row["relation"],
                "other_type": row["to_type"],
                "other_id": row["to_id"],
                "other_title": _title_for(conn, row["to_type"], row["to_id"]),
                "source": row["source"],
            })
        for row in conn.execute(
            "SELECT from_type, from_id, relation, source FROM memory_relations "
            "WHERE to_type = ? AND to_id = ? ORDER BY relation",
            (item_type, item_id),
        ).fetchall():
            out.append({
                "direction": "in",
                "relation": row["relation"],
                "other_type": row["from_type"],
                "other_id": row["from_id"],
                "other_title": _title_for(conn, row["from_type"], row["from_id"]),
                "source": row["source"],
            })
    return out


def format_relations(rels: list[dict]) -> str:
    """One compact line per edge, for injection into read-item / route output."""
    lines = []
    for r in rels:
        title = (r["other_title"] or "").strip()
        if len(title) > 80:
            title = title[:80] + "…"
        arrow = "→" if r["direction"] == "out" else "←"
        verb = r["relation"] if r["direction"] == "out" else f"{r['relation']} (of)"
        lines.append(f"  {arrow} {verb} [{r['other_type']} #{r['other_id']}] {title}")
    return "\n".join(lines)
