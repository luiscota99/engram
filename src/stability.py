"""Per-memory forgetting curves — FSRS-4.5 stability instead of one global half-life.

Before this module, ranking decayed every memory with the same fixed 90-day
half-life: a battle-tested mistake and a never-confirmed import aged
identically. FSRS (the spaced-repetition scheduler's model) gives each item
its own **stability** — "days until retrievability drops to 90%" — that grows
when the memory proves itself and shrinks when it fails:

- a **use** (``record_usage``) is a successful recall at rating *good*
- explicit **helped** feedback is a recall at rating *easy* (stronger growth)
- explicit **unhelpful** feedback is a **lapse** — and a lapse can only ever
  *reduce* stability, never raise it

Deliberately conservative integration: items with no recorded events have no
dynamics row and score **exactly as before** (the fixed-half-life path), so
seeded benchmark DBs and cold memories are untouched. Only memories with
history earn a personal curve.

Math is FSRS-4.5 (default published weights); the update rules and constants
follow the reference implementation reviewed in the July 2026 comparative
audit (docs/COMPARATIVE_REVIEW_2026-07.md). The scheduler stays deterministic
arithmetic — no LLM touches a number.
"""

from __future__ import annotations

import math

# FSRS-4.5 default weight vector.
W = [
    0.4872, 1.4003, 3.7145, 13.8206, 5.1618, 1.2298, 0.8975, 0.031,
    1.6474, 0.1367, 1.0461, 2.1072, 0.0793, 0.3246, 1.587, 0.2272, 2.8755,
]
DECAY = -0.5
FACTOR = 19.0 / 81.0  # chosen so retrievability(t=S) = 0.9

# Ratings (FSRS convention). Engram maps: use→GOOD, helped→EASY, unhelpful→lapse.
AGAIN, HARD, GOOD, EASY = 1, 2, 3, 4

STABILITY_MIN, STABILITY_MAX = 0.1, 36500.0
DIFFICULTY_MIN, DIFFICULTY_MAX = 1.0, 10.0


def retrievability(elapsed_days: float, stability: float) -> float:
    """Power-law forgetting curve: r(t=S) = 0.9 by construction."""
    if stability <= 0:
        return 0.0
    return (1.0 + FACTOR * max(0.0, elapsed_days) / stability) ** DECAY


def init_stability(rating: int) -> float:
    return max(STABILITY_MIN, W[max(AGAIN, min(EASY, rating)) - 1])


def init_difficulty(rating: int) -> float:
    d = W[4] - (rating - GOOD) * W[5]
    return max(DIFFICULTY_MIN, min(DIFFICULTY_MAX, d))


def next_difficulty(d: float, rating: int) -> float:
    """Difficulty update with mean reversion toward D0(good)."""
    nd = d - W[6] * (rating - GOOD)
    nd = W[7] * init_difficulty(GOOD) + (1 - W[7]) * nd
    return max(DIFFICULTY_MIN, min(DIFFICULTY_MAX, nd))


def next_stability_recall(d: float, s: float, r: float, rating: int) -> float:
    """Stability growth on a successful recall.

    Less growth for difficult items (11-d), diminishing returns in s
    (s^-W9), and a desirable-difficulty bonus: the lower retrievability was
    at recall time, the bigger the jump.
    """
    hard_penalty = W[15] if rating == HARD else 1.0
    easy_bonus = W[16] if rating == EASY else 1.0
    grow = (
        math.exp(W[8])
        * (11.0 - d)
        * (s ** -W[9])
        * (math.exp(W[10] * (1.0 - r)) - 1.0)
        * hard_penalty
        * easy_bonus
    )
    return max(STABILITY_MIN, min(STABILITY_MAX, s * (1.0 + grow)))


def next_stability_forget(d: float, s: float, r: float) -> float:
    """Post-lapse stability. Clamped so a lapse NEVER increases stability."""
    sf = (
        W[11]
        * (d ** -W[12])
        * (((s + 1.0) ** W[13]) - 1.0)
        * math.exp(W[14] * (1.0 - r))
    )
    return max(STABILITY_MIN, min(sf, s))


def _elapsed_days(last_event_at: str | None) -> float:
    from datetime import datetime, timezone

    if not last_event_at:
        return 0.0
    try:
        last = datetime.fromisoformat(last_event_at)
        now = datetime.now(timezone.utc) if last.tzinfo else datetime.now()
        return max(0.0, (now - last).total_seconds() / 86400.0)
    except (ValueError, TypeError):
        return 0.0


def record_event(item_type: str, item_id: int, rating: int, *, db_path=None, conn=None) -> None:
    """Apply one recall/lapse event to an item's dynamics row (upsert).

    Pure arithmetic + one upsert; never raises (a broken curve must not break
    the usage/feedback write it rides on).
    """
    from .database import connection_scope

    try:
        with connection_scope(conn, db_path) as c:
            row = c.execute(
                "SELECT stability, difficulty, last_event_at, reps, lapses "
                "FROM memory_dynamics WHERE item_type = ? AND item_id = ?",
                (item_type, int(item_id)),
            ).fetchone()

            if row is None:
                s = init_stability(rating)
                d = init_difficulty(rating)
                reps, lapses = 1, (1 if rating == AGAIN else 0)
            else:
                s, d = float(row["stability"]), float(row["difficulty"])
                elapsed = _elapsed_days(row["last_event_at"])
                r = retrievability(elapsed, s)
                if rating == AGAIN:
                    s = next_stability_forget(d, s, r)
                    lapses = int(row["lapses"]) + 1
                else:
                    s = next_stability_recall(d, s, r, rating)
                    lapses = int(row["lapses"])
                d = next_difficulty(d, rating)
                reps = int(row["reps"]) + 1

            c.execute(
                """INSERT INTO memory_dynamics
                       (item_type, item_id, stability, difficulty, last_event_at, reps, lapses)
                   VALUES (?, ?, ?, ?, datetime('now'), ?, ?)
                   ON CONFLICT(item_type, item_id) DO UPDATE SET
                       stability = excluded.stability,
                       difficulty = excluded.difficulty,
                       last_event_at = excluded.last_event_at,
                       reps = excluded.reps,
                       lapses = excluded.lapses""",
                (item_type, int(item_id), s, d, reps, lapses),
            )
    except Exception:
        import logging

        logging.getLogger(__name__).debug("stability event failed", exc_info=True)


def stability_map(
    items: list[tuple[str, int]], *, conn=None, db_path=None
) -> dict[tuple[str, int], float]:
    """Batch-fetch stabilities for rank candidates — one query, all types.

    Items without a dynamics row are absent from the map (callers fall back
    to the fixed-half-life behavior — dormancy is neutral, as always).
    """
    from .database import connection_scope

    if not items:
        return {}
    with connection_scope(conn, db_path) as c:
        placeholders = ",".join("(?,?)" for _ in items)
        flat = [v for pair in items for v in pair]
        rows = c.execute(
            f"""SELECT item_type, item_id, stability FROM memory_dynamics
                WHERE (item_type, item_id) IN (VALUES {placeholders})""",
            flat,
        ).fetchall()
    return {(r["item_type"], r["item_id"]): float(r["stability"]) for r in rows}
