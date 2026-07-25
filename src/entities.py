"""Entity linking — lightweight aliases shared across memories."""

from __future__ import annotations

import re
import unicodedata
from typing import Iterable

from .database import get_connection


def _canon(name: str) -> str:
    t = unicodedata.normalize("NFKC", (name or "").strip()).casefold()
    t = unicodedata.normalize("NFD", t)
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = re.sub(r"\s+", " ", t)
    return t


def _slug(name: str) -> str:
    c = _canon(name)
    return re.sub(r"[^a-z0-9]+", "-", c).strip("-")[:80] or "entity"


_CAMEL = re.compile(r"\b[A-Z][a-z]+(?:[A-Z][a-zA-Z0-9]+)+\b")
_IDENT = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")
_API = re.compile(r"\b(?:API|HTTP|SQL|FTS5?|MCP|WAL|JWT|OAuth|SQLite|Postgres|Redis)\b")


def extract_entity_candidates(text: str, *, limit: int = 12) -> list[str]:
    """Heuristic NER: CamelCase, snake_case, and common tech tokens."""
    if not text:
        return []
    found: list[str] = []
    seen: set[str] = set()
    for rx in (_CAMEL, _IDENT, _API):
        for m in rx.findall(text):
            key = _canon(m)
            if key in seen or len(key) < 3:
                continue
            seen.add(key)
            found.append(m if isinstance(m, str) else m[0])
            if len(found) >= limit:
                return found
    return found


def get_or_create_entity(name: str, *, conn=None, db_path=None) -> int:
    """Return entity id for *name*, creating if needed."""
    canon = _canon(name)
    if not canon:
        raise ValueError("empty entity name")
    slug = _slug(name)

    def _run(c):
        row = c.execute(
            "SELECT id FROM entities WHERE canonical_name = ?", (canon,)
        ).fetchone()
        if row:
            return int(row["id"])
        cur = c.execute(
            "INSERT INTO entities (name, canonical_name, slug) VALUES (?, ?, ?)",
            (name.strip()[:120], canon, slug),
        )
        return int(cur.lastrowid)

    if conn is not None:
        return _run(conn)
    with get_connection(db_path) as c:
        return _run(c)


def link_item_entities(
    item_type: str,
    item_id: int,
    names: Iterable[str],
    *,
    conn=None,
    db_path=None,
) -> list[int]:
    """Link an item to entities (create as needed). Returns entity ids."""

    def _run(c):
        ids: list[int] = []
        for name in names:
            try:
                eid = get_or_create_entity(name, conn=c)
            except ValueError:
                continue
            c.execute(
                "INSERT OR IGNORE INTO item_entities (item_type, item_id, entity_id) "
                "VALUES (?, ?, ?)",
                (item_type, int(item_id), eid),
            )
            ids.append(eid)
        return ids

    if conn is not None:
        return _run(conn)
    with get_connection(db_path) as c:
        return _run(c)


def entities_for_item(item_type: str, item_id: int, *, conn=None, db_path=None) -> list[dict]:
    def _run(c):
        rows = c.execute(
            """SELECT e.id, e.name, e.canonical_name, e.slug
               FROM item_entities ie
               JOIN entities e ON e.id = ie.entity_id
               WHERE ie.item_type = ? AND ie.item_id = ?
               ORDER BY e.name""",
            (item_type, int(item_id)),
        ).fetchall()
        return [dict(r) for r in rows]

    if conn is not None:
        return _run(conn)
    with get_connection(db_path) as c:
        return _run(c)


def entity_ids_for_query(query: str, *, conn=None, db_path=None) -> set[int]:
    """Match query tokens/aliases against known entities."""
    q = _canon(query)
    if not q:
        return set()

    def _run(c):
        rows = c.execute("SELECT id, canonical_name, name FROM entities").fetchall()
        hits: set[int] = set()
        for row in rows:
            cn = row["canonical_name"] or ""
            if cn and (cn in q or any(tok and tok in q for tok in cn.split())):
                hits.add(int(row["id"]))
        return hits

    if conn is not None:
        return _run(conn)
    with get_connection(db_path) as c:
        return _run(c)


def item_entity_map(
    items: list[tuple[str, int]], *, conn=None, db_path=None
) -> dict[tuple[str, int], set[int]]:
    """Batch entity membership for candidate items."""
    if not items:
        return {}

    def _run(c):
        placeholders = ",".join("(?,?)" for _ in items)
        flat = [v for pair in items for v in pair]
        rows = c.execute(
            f"""SELECT item_type, item_id, entity_id FROM item_entities
                WHERE (item_type, item_id) IN (VALUES {placeholders})""",
            flat,
        ).fetchall()
        out: dict[tuple[str, int], set[int]] = {pair: set() for pair in items}
        for r in rows:
            key = (r["item_type"], int(r["item_id"]))
            out.setdefault(key, set()).add(int(r["entity_id"]))
        return out

    if conn is not None:
        return _run(conn)
    with get_connection(db_path) as c:
        return _run(c)


def auto_link_from_text(
    item_type: str, item_id: int, text: str, *, conn=None, db_path=None
) -> list[int]:
    return link_item_entities(
        item_type, item_id, extract_entity_candidates(text), conn=conn, db_path=db_path
    )
