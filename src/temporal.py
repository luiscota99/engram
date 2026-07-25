"""Temporal invalidation — supersede stale memory entries."""

from __future__ import annotations

import logging
from datetime import date

from .database import get_connection, index_in_fts, table_for

logger = logging.getLogger(__name__)

INVALIDATABLE_TYPES = frozenset({"mistake", "pattern", "skill"})


def invalidate_memory(
    item_type: str,
    item_id: int,
    *,
    superseded_by: int | None = None,
    reason: str | None = None,
    db_path=None,
) -> bool:
    """Mark a memory item as superseded; demote in FTS ranking."""
    if item_type not in INVALIDATABLE_TYPES:
        return False
    table = table_for(item_type)
    if not table:
        return False

    with get_connection(db_path) as conn:
        row = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (item_id,)).fetchone()
        if not row:
            return False

        try:
            conn.execute(
                f"UPDATE {table} SET superseded_by = ? WHERE id = ?",
                (superseded_by, item_id),
            )
        except Exception:
            logger.warning(
                "Failed to set %s.superseded_by for id=%s", table, item_id, exc_info=True
            )

        title_col = {
            "mistake": "mistake",
            "pattern": "name",
            "skill": "name",
        }[item_type]
        title = row[title_col]
        if not str(title).startswith("[SUPERSEDED]"):
            new_title = f"[SUPERSEDED] {title}"[:200]
            conn.execute(
                f"UPDATE {table} SET {title_col} = ? WHERE id = ?",
                (new_title, item_id),
            )
            title = new_title

        fts_row = conn.execute(
            "SELECT rowid, content, tags FROM memory_fts WHERE item_type = ? AND item_id = ?",
            (item_type, str(item_id)),
        ).fetchone()
        if fts_row:
            conn.execute("DELETE FROM vec_memory WHERE rowid = ?", (fts_row["rowid"],))
            tags = (fts_row["tags"] or "").split()
            index_in_fts(
                conn,
                item_type,
                item_id,
                title[:80] if item_type == "mistake" else title,
                fts_row["content"] or "",
                tags,
            )

        if reason:
            conn.execute(
                """INSERT INTO memory_facts
                   (subject, predicate, object, valid_until, source_type, source_id)
                   VALUES (?, 'invalidated', ?, ?, ?, ?)""",
                (
                    f"{item_type}:{item_id}",
                    reason,
                    date.today().isoformat(),
                    item_type,
                    item_id,
                ),
            )

    if superseded_by:
        try:
            from .relations import add_relation

            add_relation(
                item_type, int(superseded_by), item_type, item_id, "supersedes",
                source="merge", db_path=db_path, validate_exists=False,
            )
        except Exception:
            logger.debug("failed to record supersedes edge", exc_info=True)
    return True


def invalidated_subjects_as_of(as_of: str, *, conn=None, db_path=None) -> set[str]:
    """Return ``type:id`` subjects invalidated on or before *as_of* (ISO date)."""
    as_of = (as_of or date.today().isoformat())[:10]

    def _run(c):
        rows = c.execute(
            """SELECT subject FROM memory_facts
               WHERE predicate = 'invalidated'
                 AND valid_until IS NOT NULL
                 AND substr(valid_until, 1, 10) <= ?""",
            (as_of,),
        ).fetchall()
        return {r["subject"] for r in rows if r["subject"]}

    if conn is not None:
        return _run(conn)
    with get_connection(db_path) as c:
        return _run(c)


def kg_query(subject: str | None = None, *, limit: int = 50, db_path=None) -> list[dict]:
    """List memory_facts, optionally filtered by subject prefix."""
    with get_connection(db_path) as conn:
        if subject:
            rows = conn.execute(
                """SELECT id, subject, predicate, object, valid_from, valid_until,
                          source_type, source_id
                   FROM memory_facts
                   WHERE subject = ? OR subject LIKE ?
                   ORDER BY id DESC LIMIT ?""",
                (subject, f"{subject}%", limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT id, subject, predicate, object, valid_from, valid_until,
                          source_type, source_id
                   FROM memory_facts
                   ORDER BY id DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]


def kg_timeline(subject: str, *, db_path=None) -> list[dict]:
    """Chronological facts for a subject (validity windows)."""
    with get_connection(db_path) as conn:
        rows = conn.execute(
            """SELECT id, subject, predicate, object, valid_from, valid_until,
                      source_type, source_id
               FROM memory_facts
               WHERE subject = ?
               ORDER BY valid_from ASC, id ASC""",
            (subject,),
        ).fetchall()
        return [dict(r) for r in rows]


def upsert_fact(
    subject: str,
    predicate: str,
    obj: str,
    *,
    valid_from: str | None = None,
    valid_until: str | None = None,
    source_type: str | None = None,
    source_id: int | None = None,
    db_path=None,
) -> int:
    """Insert a temporal fact row. Returns new id."""
    with get_connection(db_path) as conn:
        cur = conn.execute(
            """INSERT INTO memory_facts
               (subject, predicate, object, valid_from, valid_until, source_type, source_id)
               VALUES (?, ?, ?, COALESCE(?, date('now')), ?, ?, ?)""",
            (
                subject,
                predicate,
                obj,
                valid_from,
                valid_until,
                source_type,
                source_id,
            ),
        )
        return int(cur.lastrowid)
