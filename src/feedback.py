"""Retrieval feedback — reward and discourage memories in RANKING, never in existence.

The precision problem this solves: hybrid search always returns a nearest
neighbor, so an item that keeps surfacing for queries it doesn't help with
crowds out better answers. Feedback is the correction signal: ``helped``
rewards an item, ``unhelpful`` demotes it — in ranking only.

Two invariants, set by the user:

* **Non-use is not a signal.** Knowledge can sit dormant for months and stay
  exactly where it is — you can't leverage everything all the time. Only
  explicit negative feedback ever counts against an item.
* **Deletion is the user's decision.** Net-negative items are PROPOSED for
  archival through the inbox (``propose_negative_item_reviews``); nothing in
  this module deletes, archives, or invalidates anything.
"""

from __future__ import annotations

# Demotion is deliberately stronger than reward (precision over recall — the
# same stance as the recall/guard gates): one "unhelpful" outweighs one
# "helped". Both log-scaled like the usage boost so pile-ons can't drown
# lexical/semantic relevance entirely.
HELPED_WEIGHT = 12.0
UNHELPFUL_WEIGHT = 18.0

# An item is proposed for user review (never auto-deleted) once its net
# feedback reaches this many more unhelpful than helped marks.
REVIEW_NET_THRESHOLD = -2

VALID_ITEM_TYPES = ("mistake", "pattern", "skill", "conversation", "prompt", "session")


def add_feedback(
    item_type: str,
    item_id: int,
    *,
    helpful: bool,
    query: str = "",
    source: str = "manual",
    db_path=None,
) -> bool:
    """Record one feedback event. Returns False for an unknown type/id."""
    from .database import get_connection, get_item

    if item_type not in VALID_ITEM_TYPES:
        return False
    if get_item(item_type, item_id, db_path=db_path) is None:
        return False
    with get_connection(db_path) as conn:
        conn.execute(
            """INSERT INTO retrieval_feedback (item_type, item_id, helpful, query, source)
               VALUES (?, ?, ?, ?, ?)""",
            (item_type, int(item_id), 1 if helpful else -1, (query or "")[:500], source),
        )
    return True


def feedback_totals(
    items: list[tuple[str, int]], *, conn=None, db_path=None
) -> dict[tuple[str, int], tuple[int, int]]:
    """Batch (helped, unhelpful) totals for (item_type, item_id) pairs.

    One query for the whole candidate set — every item type lives in the same
    table, so there is no per-type participation drift and no N+1.
    """
    from .database import get_connection

    if not items:
        return {}

    def _fetch(c):
        placeholders = ",".join("(?,?)" for _ in items)
        flat = [v for pair in items for v in pair]
        rows = c.execute(
            f"""SELECT item_type, item_id,
                       SUM(CASE WHEN helpful = 1 THEN 1 ELSE 0 END) AS helped,
                       SUM(CASE WHEN helpful = -1 THEN 1 ELSE 0 END) AS unhelpful
                FROM retrieval_feedback
                WHERE (item_type, item_id) IN (VALUES {placeholders})
                GROUP BY item_type, item_id""",
            flat,
        ).fetchall()
        return {
            (r["item_type"], r["item_id"]): (r["helped"] or 0, r["unhelpful"] or 0)
            for r in rows
        }

    if conn is not None:
        return _fetch(conn)
    with get_connection(db_path) as c:
        return _fetch(c)


def feedback_score(helped: int, unhelpful: int) -> float:
    """Additive ranking term: log-scaled reward minus a stronger demotion."""
    import math

    return HELPED_WEIGHT * math.log1p(max(0, helped)) - UNHELPFUL_WEIGHT * math.log1p(
        max(0, unhelpful)
    )


def net_negative_items(*, threshold: int = REVIEW_NET_THRESHOLD, db_path=None) -> list[dict]:
    """Items whose net feedback (helped - unhelpful) is at or below *threshold*.

    These are candidates to PROPOSE for user review — mere non-use never
    qualifies (an item with zero feedback is simply dormant, which is fine).
    """
    from .database import get_connection

    with get_connection(db_path) as conn:
        rows = conn.execute(
            """SELECT item_type, item_id,
                      SUM(CASE WHEN helpful = 1 THEN 1 ELSE 0 END) AS helped,
                      SUM(CASE WHEN helpful = -1 THEN 1 ELSE 0 END) AS unhelpful
               FROM retrieval_feedback
               GROUP BY item_type, item_id
               HAVING (helped - unhelpful) <= ?
               ORDER BY (helped - unhelpful) ASC""",
            (threshold,),
        ).fetchall()
    return [dict(r) for r in rows]
