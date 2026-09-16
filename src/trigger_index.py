"""Deterministic trigger index — the guard's fast path (schema v28).

The July 31 ROI verdict convicted the guard as engram's dominant cost: 82% of
all searches, 73% of injected tokens, a ~1.4s embedding call on 99% of tool
actions — for warnings whose measured value came from lexical matches. This
module replaces that per-action hybrid search with an O(1)-shaped probe:

- At write time, each mistake/pattern (the only types the guard warns on)
  contributes its normalized word 2/3-gram shingles to ``trigger_ngrams``.
- At guard time, the action text is normalized and shingled the same way and
  matched with ONE indexed query. An item warns only when it shares at least
  ``MIN_SHINGLE_HITS`` distinct shingles — precision over recall, as always.

No embedding, no model, no network: microseconds instead of seconds, and the
fire rate becomes a real signal instead of a 99% constant. Maintained by the
same single owners as FTS (``index_in_fts`` / ``delete_item``) — never by
triggers, per the v23 lesson.
"""

from __future__ import annotations

import re
import unicodedata

# An item must share this many DISTINCT shingles with the action to warn.
MIN_SHINGLE_HITS = 2

# The guard only ever warns on cautionary types (see hooks.build_guard_warnings).
TRIGGER_TYPES = ("mistake", "pattern")

MAX_WARNINGS = 3

# Minimal bilingual stopword set: shingles made ONLY of these never index or
# probe — "in the"-grade collisions are noise, not triggers.
_STOPWORDS = frozenset(
    "the a an of in on to for and or is are was with that this it de la el los "
    "las un una y o en del al con que es por para se su lo como no si".split()
)

_WORD_RE = re.compile(r"[a-z0-9]{2,}")


def normalize(text: str) -> list[str]:
    """NFKC → strip accents → casefold → word tokens (≥2 chars).

    Accent stripping keeps Spanish/English shingles stable ("verificación" and
    "verificacion" produce the same trigger).
    """
    text = unicodedata.normalize("NFKC", text or "")
    text = "".join(c for c in unicodedata.normalize("NFD", text) if not unicodedata.combining(c))
    return _WORD_RE.findall(text.casefold())


def shingles(text: str, *, max_shingles: int = 400) -> set[str]:
    """Word 2-grams and 3-grams of normalized text, stopword-only ones dropped."""
    words = normalize(text)
    out: set[str] = set()
    for n in (2, 3):
        for i in range(len(words) - n + 1):
            gram = words[i : i + n]
            if all(w in _STOPWORDS for w in gram):
                continue
            out.add(" ".join(gram))
            if len(out) >= max_shingles:
                return out
    return out


def index_item_triggers(conn, item_type: str, item_id: int, text: str) -> None:
    """(Re)build the trigger rows for one item. No-op for non-trigger types."""
    if item_type not in TRIGGER_TYPES:
        return
    conn.execute(
        "DELETE FROM trigger_ngrams WHERE item_type = ? AND item_id = ?",
        (item_type, int(item_id)),
    )
    grams = shingles(text)
    if grams:
        conn.executemany(
            "INSERT OR IGNORE INTO trigger_ngrams (ngram, item_type, item_id) VALUES (?, ?, ?)",
            [(g, item_type, int(item_id)) for g in grams],
        )


def probe(conn, action_text: str, *, limit: int = MAX_WARNINGS) -> list[dict]:
    """Items sharing ≥MIN_SHINGLE_HITS distinct shingles with the action.

    One indexed query over the action's shingles (bounded set — the guard
    already clips its query text), ordered by overlap. Returns
    ``[{item_type, item_id, hits}]``; titles are the caller's concern.
    """
    grams = shingles(action_text)
    if not grams:
        return []
    placeholders = ",".join("?" * len(grams))
    rows = conn.execute(
        f"""SELECT item_type, item_id, COUNT(DISTINCT ngram) AS hits
            FROM trigger_ngrams WHERE ngram IN ({placeholders})
            GROUP BY item_type, item_id
            HAVING hits >= ?
            ORDER BY hits DESC, item_type, item_id
            LIMIT ?""",
        [*grams, MIN_SHINGLE_HITS, limit],
    ).fetchall()
    return [dict(r) for r in rows]


def backfill(conn) -> int:
    """Index every existing trigger-type item (migration v28 / repair).

    Reads title+content from the FTS table — the same text the old search
    path matched against, so warning behavior stays comparable.
    """
    total = 0
    for item_type in TRIGGER_TYPES:
        rows = conn.execute(
            "SELECT item_id, title, content FROM memory_fts WHERE item_type = ?",
            (item_type,),
        ).fetchall()
        for r in rows:
            index_item_triggers(
                conn, item_type, int(r["item_id"]), f"{r['title']} {r['content'] or ''}"
            )
            total += 1
    return total
