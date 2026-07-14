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
            # Supersession still proceeds via the [SUPERSEDED] title below,
            # but losing the link is worth surfacing — it hid a missing-column
            # bug on fresh installs once already.
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

    # Record the supersession as a typed edge so recall can traverse it — the
    # keeper `supersedes` the item just invalidated. Auto-derived, zero friction.
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
