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
    "not_related": "`from` and `to` were judged NOT related — a recorded distinction "
                   "so a spurious link isn't re-suggested",
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


# Relations that are single-valued on the `from` side: asserting a new one
# retires any prior active edge of the same (from, relation) pointing elsewhere.
# None of the closed vocabulary qualifies by default — notably `supersedes` is
# one-to-MANY here (consolidation merges a cluster: one keeper supersedes every
# loser, and all those edges are simultaneously true). Callers that genuinely
# want replace-on-change opt in per call via functional=True.
FUNCTIONAL_RELATIONS: set[str] = set()

# Provenance is CODE-set, never model-set (anti-poisoning): the write path decides
# it, not the content being stored.
PROVENANCE_KINDS = {"manual", "merge", "system"}


def add_relation(
    from_type: str,
    from_id: int,
    to_type: str,
    to_id: int,
    relation: str,
    *,
    source: str = "manual",
    actor: str | None = None,
    provenance: str = "manual",
    valid_from: str | None = None,
    functional: bool = False,
    db_path=None,
    validate_exists: bool = True,
) -> str | None:
    """Create (or re-activate) a typed edge. Returns an error string, or None.

    Race-safe by CAS: a single UPSERT against the UNIQUE(edge) constraint, so
    concurrent writers never double-insert and re-asserting an invalidated edge
    re-activates it (no check-then-act). ``actor`` records who asserted it and
    ``provenance`` (manual|merge|system) how it was learned — both code-set, an
    anti-poisoning layer. ``valid_from`` is the event-time the fact began.
    ``functional`` (or a relation in FUNCTIONAL_RELATIONS) retires any prior
    active edge of the same (from, relation) pointing elsewhere (supersede-on-change).
    """
    if relation not in RELATION_TYPES:
        return f"Unknown relation '{relation}'. Choose from: {', '.join(sorted(RELATION_TYPES))}."
    if from_type not in _ITEM_TABLES or to_type not in _ITEM_TABLES:
        return f"Item types must be one of: {', '.join(sorted(_ITEM_TABLES))}."
    if (from_type, from_id) == (to_type, to_id):
        return "An item cannot relate to itself."
    if provenance not in PROVENANCE_KINDS:
        return f"Unknown provenance '{provenance}'. Choose from: {', '.join(sorted(PROVENANCE_KINDS))}."

    with get_connection(db_path) as conn:
        if validate_exists:
            if _title_for(conn, from_type, from_id) is None:
                return f"No {from_type} with id {from_id}."
            if _title_for(conn, to_type, to_id) is None:
                return f"No {to_type} with id {to_id}."
        if functional or relation in FUNCTIONAL_RELATIONS:
            conn.execute(
                "UPDATE memory_relations SET status='invalidated', "
                "invalidated_at=datetime('now'), valid_to=COALESCE(valid_to, datetime('now')) "
                "WHERE from_type=? AND from_id=? AND relation=? AND status='active' "
                "AND NOT (to_type=? AND to_id=?)",
                (from_type, from_id, relation, to_type, to_id),
            )
        conn.execute(
            "INSERT INTO memory_relations "
            "(from_type, from_id, to_type, to_id, relation, source, actor, provenance, valid_from, recorded_at, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), 'active') "
            "ON CONFLICT(from_type, from_id, to_type, to_id, relation) DO UPDATE SET "
            "status='active', invalidated_at=NULL, valid_to=NULL, recorded_at=datetime('now'), "
            "actor=excluded.actor, provenance=excluded.provenance, valid_from=excluded.valid_from, "
            "source=excluded.source",
            (from_type, from_id, to_type, to_id, relation, source, actor, provenance, valid_from),
        )
    return None


def invalidate_relation(
    from_type: str, from_id: int, to_type: str, to_id: int, relation: str, db_path=None
) -> bool:
    """Retire an active edge (bi-temporal close): status=invalidated, stamp
    invalidated_at and valid_to. Keeps the row for "what was true when" queries.
    Returns True if an active edge was retired."""
    with get_connection(db_path) as conn:
        cur = conn.execute(
            "UPDATE memory_relations SET status='invalidated', "
            "invalidated_at=datetime('now'), valid_to=COALESCE(valid_to, datetime('now')) "
            "WHERE from_type=? AND from_id=? AND to_type=? AND to_id=? AND relation=? AND status='active'",
            (from_type, from_id, to_type, to_id, relation),
        )
        return cur.rowcount > 0


def _asof_clause(as_of: str | None, include_invalidated: bool) -> tuple[str, list]:
    """SQL predicate (+params) selecting edges visible under the temporal filter.

    - as_of set: bi-temporal point query over all 4 axes (edge recorded by then,
      not yet retired, and its event-time window covers `as_of`).
    - as_of None: active edges only, unless include_invalidated.
    """
    if as_of is not None:
        return (
            " AND recorded_at <= ? AND (invalidated_at IS NULL OR invalidated_at > ?)"
            " AND (valid_from IS NULL OR valid_from <= ?)"
            " AND (valid_to IS NULL OR valid_to > ?)",
            [as_of, as_of, as_of, as_of],
        )
    if include_invalidated:
        return ("", [])
    return (" AND status = 'active'", [])


def get_relations(
    item_type: str,
    item_id: int,
    db_path=None,
    *,
    as_of: str | None = None,
    include_invalidated: bool = False,
) -> list[dict]:
    """All edges touching an item, each with the other endpoint's title.

    Returns dicts: ``{direction, relation, other_type, other_id, other_title,
    source, status, actor, provenance, valid_from, valid_to}``. Outgoing first.
    By default only ``active`` edges; pass ``as_of`` for a "what was true when"
    point query, or ``include_invalidated`` for the full history.
    """
    filt, fparams = _asof_clause(as_of, include_invalidated)
    out: list[dict] = []
    cols = ("relation, source, status, actor, provenance, "
            "valid_from, valid_to, recorded_at, invalidated_at")
    with get_connection(db_path) as conn:
        for row in conn.execute(
            f"SELECT to_type, to_id, {cols} FROM memory_relations "
            f"WHERE from_type = ? AND from_id = ?{filt} ORDER BY relation",
            (item_type, item_id, *fparams),
        ).fetchall():
            out.append({
                "direction": "out",
                "relation": row["relation"],
                "other_type": row["to_type"],
                "other_id": row["to_id"],
                "other_title": _title_for(conn, row["to_type"], row["to_id"]),
                "source": row["source"],
                "status": row["status"],
                "actor": row["actor"],
                "provenance": row["provenance"],
                "valid_from": row["valid_from"],
                "valid_to": row["valid_to"],
                "recorded_at": row["recorded_at"],
                "invalidated_at": row["invalidated_at"],
            })
        for row in conn.execute(
            f"SELECT from_type, from_id, {cols} FROM memory_relations "
            f"WHERE to_type = ? AND to_id = ?{filt} ORDER BY relation",
            (item_type, item_id, *fparams),
        ).fetchall():
            out.append({
                "direction": "in",
                "relation": row["relation"],
                "other_type": row["from_type"],
                "other_id": row["from_id"],
                "other_title": _title_for(conn, row["from_type"], row["from_id"]),
                "source": row["source"],
                "status": row["status"],
                "actor": row["actor"],
                "provenance": row["provenance"],
                "valid_from": row["valid_from"],
                "valid_to": row["valid_to"],
                "recorded_at": row["recorded_at"],
                "invalidated_at": row["invalidated_at"],
            })
    return out


def remove_relation(
    from_type: str, from_id: int, to_type: str, to_id: int, relation: str, db_path=None
) -> bool:
    """Delete one edge. Returns True if a row was removed."""
    with get_connection(db_path) as conn:
        cur = conn.execute(
            "DELETE FROM memory_relations WHERE from_type=? AND from_id=? "
            "AND to_type=? AND to_id=? AND relation=?",
            (from_type, from_id, to_type, to_id, relation),
        )
        return cur.rowcount > 0


def _has_relation(conn, a: tuple, b: tuple, relation: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM memory_relations WHERE from_type=? AND from_id=? "
        "AND to_type=? AND to_id=? AND relation=? LIMIT 1",
        (a[0], a[1], b[0], b[1], relation),
    ).fetchone() is not None


def find_relationship_questions(db_path=None, limit: int = 10) -> list[dict]:
    """Edges worth a human decision: links between items in DISJOINT domains
    (no shared tags) — "why is this related to that?". These are the spurious /
    incidental / one-directional links the user should confirm or reject.

    A recorded ``not_related`` on the pair (either direction) resolves it, so it
    is never asked again. Each result carries the two endpoints + the resolve
    commands the inbox will present.
    """
    from .database import get_tags_for_item

    out: list[dict] = []
    seen: set = set()
    with get_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT from_type, from_id, to_type, to_id, relation FROM memory_relations "
            "WHERE relation != 'not_related' ORDER BY created_at DESC"
        ).fetchall()
        for r in rows:
            a = (r["from_type"], r["from_id"])
            b = (r["to_type"], r["to_id"])
            key = tuple(sorted([a, b]))
            if key in seen:
                continue
            if _has_relation(conn, a, b, "not_related") or _has_relation(conn, b, a, "not_related"):
                continue
            ta = set(get_tags_for_item(conn, a[0], a[1]))
            tb = set(get_tags_for_item(conn, b[0], b[1]))
            # Both tagged, but no shared tag → cross-domain link → question.
            if not (ta and tb) or (ta & tb):
                continue
            seen.add(key)
            out.append({
                "from_type": a[0], "from_id": a[1],
                "to_type": b[0], "to_id": b[1],
                "relation": r["relation"],
                "from_title": _title_for(conn, a[0], a[1]),
                "to_title": _title_for(conn, b[0], b[1]),
                "from_tags": sorted(ta), "to_tags": sorted(tb),
            })
            if len(out) >= limit:
                break
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
