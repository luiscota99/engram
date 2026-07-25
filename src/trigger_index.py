"""Trigger n-gram index for the guard hot path (no embeddings).

Precision-over-recall: only fire when a canonicalized action string contains a
stored trigger phrase extracted from mistakes/patterns at write time.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Iterable

from .database import get_connection

_STOP = frozenset(
    "this that with from into your have been were will would could should about "
    "when then than them they their what which while where also just only over "
    "under after before using used use for the and not but are was".split()
)


def canonicalize(text: str) -> str:
    """NFKC + casefold + accent-fold + whitespace collapse."""
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", text).casefold()
    t = unicodedata.normalize("NFD", t)
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = re.sub(r"[_\W]+", " ", t, flags=re.UNICODE)
    return " ".join(t.split())


def extract_trigger_phrases(title: str, content: str = "", *, max_phrases: int = 12) -> list[str]:
    """Derive short trigger phrases from a memory's title/content."""
    blob = canonicalize(f"{title} {content}")
    tokens = [tok for tok in blob.split() if len(tok) >= 4 and tok not in _STOP]
    phrases: list[str] = []
    seen: set[str] = set()

    def _add(p: str) -> None:
        p = p.strip()
        if len(p) < 6 or p in seen:
            return
        seen.add(p)
        phrases.append(p)

    # Prefer multi-word n-grams (more precise), then distinctive unigrams.
    for n in (3, 2):
        for i in range(max(0, len(tokens) - n + 1)):
            _add(" ".join(tokens[i : i + n]))
            if len(phrases) >= max_phrases:
                return phrases
    for tok in tokens:
        _add(tok)
        if len(phrases) >= max_phrases:
            break
    return phrases


def rebuild_triggers_for_item(
    conn,
    item_type: str,
    item_id: int,
    title: str,
    content: str = "",
) -> int:
    """Replace trigger rows for one item. Returns number of phrases stored."""
    if item_type not in ("mistake", "pattern"):
        return 0
    conn.execute(
        "DELETE FROM mistake_triggers WHERE item_type = ? AND item_id = ?",
        (item_type, int(item_id)),
    )
    phrases = extract_trigger_phrases(title, content)
    for phrase in phrases:
        conn.execute(
            "INSERT OR IGNORE INTO mistake_triggers (item_type, item_id, phrase) VALUES (?, ?, ?)",
            (item_type, int(item_id), phrase),
        )
    return len(phrases)


def reindex_all_triggers(db_path=None) -> int:
    """Rebuild the whole trigger table from mistakes + patterns. Returns phrase count."""
    total = 0
    with get_connection(db_path) as conn:
        conn.execute("DELETE FROM mistake_triggers")
        for itype, table, title_col, content_expr in (
            ("mistake", "mistakes", "mistake", "context || ' ' || mistake || ' ' || fix"),
            ("pattern", "patterns", "name", "symptoms || ' ' || root_cause || ' ' || standard_fix"),
        ):
            rows = conn.execute(
                f"SELECT id, {title_col} AS title, {content_expr} AS content FROM {table}"
            ).fetchall()
            for row in rows:
                total += rebuild_triggers_for_item(
                    conn, itype, int(row["id"]), row["title"] or "", row["content"] or ""
                )
    return total


def probe_triggers(
    action_text: str,
    *,
    limit: int = 5,
    db_path=None,
) -> list[dict]:
    """O(phrases) substring probe — no embeddings. Empty when nothing matches."""
    hay = canonicalize(action_text)
    if len(hay) < 6:
        return []
    with get_connection(db_path) as conn:
        try:
            rows = conn.execute(
                "SELECT item_type, item_id, phrase FROM mistake_triggers"
            ).fetchall()
        except Exception:
            return []
    hits: list[dict] = []
    seen: set[tuple[str, int]] = set()
    # Longer phrases first for better precision.
    ordered = sorted(rows, key=lambda r: len(r["phrase"] or ""), reverse=True)
    for row in ordered:
        phrase = row["phrase"] or ""
        key = (row["item_type"], int(row["item_id"]))
        if key in seen:
            continue
        if phrase and phrase in hay:
            seen.add(key)
            hits.append(
                {
                    "item_type": row["item_type"],
                    "item_id": int(row["item_id"]),
                    "phrase": phrase,
                    "match_kind": "trigger",
                }
            )
            if len(hits) >= limit:
                break
    return hits


def enrich_trigger_hits(hits: Iterable[dict], db_path=None) -> list[dict]:
    """Attach titles for display."""
    from .database import get_item

    out = []
    for h in hits:
        item = get_item(h["item_type"], h["item_id"], db_path=db_path) or {}
        title = (
            item.get("mistake")
            or item.get("name")
            or item.get("title")
            or h.get("phrase")
            or "(untitled)"
        )
        out.append({**h, "title": str(title)[:120]})
    return out
